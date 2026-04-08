"""
strategy/retest_engine.py — Retest confirmation state machine.

Owns the complete logic for breakout detection, retest validation,
and entry confirmation. Both LONG and SHORT directions track
independent state and are evaluated on every bar.

STATE PER DIRECTION
===================
Each direction (LONG / SHORT) progresses through:

  IDLE
    → N closes outside boundary (breakout_bars)
  BREAKOUT_CONFIRMED
    → price enters window, stays inside for N bars (retest_bars)
      OR opposing breakout confirmed (implicit credit via expansion)
  RETEST_CREDITED
    → N closes back outside boundary (reconfirm_bars)
  ENTRY

WINDOW EXPANSION
================
Triggered when the OPPOSING breakout is confirmed. The boundary on
the confirmed side expands to the confirmed breakout's extreme price.
Simultaneously grants implicit retest credit to the original direction.

Example:
  Window: 683–688
  LONG confirmed at 690 → window stays 683–688 (no expansion yet)
  SHORT confirmed at 682 → window becomes 683–690
    - HIGH boundary moves to 690 (LONG breakout extreme)
    - LOW boundary stays at 683 (SHORT not yet confirmed on this side)
    - LONG retest credited implicitly

PARAMETERS
==========
  breakout_bars  : consecutive closes outside boundary to confirm breakout
  retest_bars    : consecutive closes inside window to credit retest
  reconfirm_bars : consecutive closes outside boundary after retest to enter
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result returned to ORBBase
# ---------------------------------------------------------------------------

@dataclass
class RetestResult:
    """
    Returned by RetestEngine.on_bar() when a notable event occurs.
    None is returned when nothing changed.
    """
    direction:   str    # "LONG" or "SHORT"
    entry_price: float  # underlying close price at event
    event:       str    # "BREAKOUT" | "RETEST" | "EXPAND" | "ENTRY"
    range_high:  float  # current window high after any expansion
    range_low:   float  # current window low after any expansion


# ---------------------------------------------------------------------------
# Per-direction state
# ---------------------------------------------------------------------------

@dataclass
class DirectionState:
    # Phase flags
    breakout_confirmed: bool  = False
    retest_credited:    bool  = False
    entry_fired:        bool  = False
    # Counters
    breakout_count:     int   = 0   # consecutive closes outside boundary
    retest_count:       int   = 0   # consecutive closes inside window
    reconfirm_count:    int   = 0   # consecutive closes outside after retest
    # Breakout extreme — used for window expansion
    breakout_extreme:   float = 0.0


# ---------------------------------------------------------------------------
# Retest engine
# ---------------------------------------------------------------------------

class RetestEngine:
    """
    Stateful retest confirmation engine. One instance per trading day
    (reset by ORBBase on day boundary).

    Args:
        breakout_bars  : consecutive closes outside range to confirm breakout
        retest_bars    : consecutive closes inside window to credit retest
        reconfirm_bars : consecutive closes outside window after retest to enter
    """

    def __init__(
        self,
        breakout_bars:  int = 3,
        retest_bars:    int = 3,
        reconfirm_bars: int = 3,
    ):
        self.breakout_bars  = breakout_bars
        self.retest_bars    = retest_bars
        self.reconfirm_bars = reconfirm_bars

        self.long  = DirectionState()
        self.short = DirectionState()

        # Live window boundaries — may expand during the session
        self._range_high: float = 0.0
        self._range_low:  float = 0.0
        self._initialized: bool = False

        # Track which direction confirmed first — only that direction's
        # extreme is used for window expansion
        self._first_confirmed: str | None = None   # "LONG" or "SHORT"

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def set_range(self, range_high: float, range_low: float) -> None:
        """
        Set initial range boundaries. Called by ORBBase when range is set.
        """
        self._range_high  = range_high
        self._range_low   = range_low
        self._initialized = True

    @property
    def range_high(self) -> float:
        return self._range_high

    @property
    def range_low(self) -> float:
        return self._range_low

    def on_bar(self, bar_close: float) -> list[RetestResult]:
        """
        Process one bar. Returns a list of RetestResult events that
        occurred on this bar (usually empty or one item, occasionally
        two if both directions trigger simultaneously).

        Caller should process results in order and act on ENTRY events.
        """
        if not self._initialized:
            return []

        results = []

        above  = bar_close > self._range_high
        below  = bar_close < self._range_low
        inside = not above and not below

        # ---- update both directions ----
        long_result  = self._update_direction(
            state     = self.long,
            direction = "LONG",
            on_side   = above,
            inside    = inside,
            bar_close = bar_close,
        )
        short_result = self._update_direction(
            state     = self.short,
            direction = "SHORT",
            on_side   = below,
            inside    = inside,
            bar_close = bar_close,
        )

        # ---- check window expansion ----
        # Expansion only happens when BOTH directions are confirmed.
        # Only the FIRST-confirmed direction's extreme expands its boundary.
        # The second-confirmed direction uses the original boundary.
        #
        # LONG confirmed first → when SHORT also confirms:
        #   HIGH expands to LONG's extreme, LONG retest credited
        #   LOW stays unchanged
        #
        # SHORT confirmed first → when LONG also confirms:
        #   LOW expands to SHORT's extreme, SHORT retest credited
        #   HIGH stays unchanged

        if (
            self.long.breakout_confirmed
            and self.short.breakout_confirmed
            and self._first_confirmed == "LONG"
            and not self.long.retest_credited
            and self.long.breakout_extreme > self._range_high
        ):
            old_high              = self._range_high
            self._range_high      = self.long.breakout_extreme
            self.long.retest_credited = True
            self.long.retest_count    = 0
            results.append(RetestResult(
                direction   = "LONG",
                entry_price = bar_close,
                event       = "EXPAND",
                range_high  = self._range_high,
                range_low   = self._range_low,
            ))
            logger.info(
                f"  WINDOW EXPAND (LONG first, SHORT confirmed): "
                f"high {old_high:.4f} → {self._range_high:.4f} | "
                f"LONG retest credited implicitly"
            )

        if (
            self.short.breakout_confirmed
            and self.long.breakout_confirmed
            and self._first_confirmed == "SHORT"
            and not self.short.retest_credited
            and self.short.breakout_extreme < self._range_low
        ):
            old_low               = self._range_low
            self._range_low       = self.short.breakout_extreme
            self.short.retest_credited = True
            self.short.retest_count    = 0
            results.append(RetestResult(
                direction   = "SHORT",
                entry_price = bar_close,
                event       = "EXPAND",
                range_high  = self._range_high,
                range_low   = self._range_low,
            ))
            logger.info(
                f"  WINDOW EXPAND (SHORT first, LONG confirmed): "
                f"low {old_low:.4f} → {self._range_low:.4f} | "
                f"SHORT retest credited implicitly"
            )

        if long_result:
            results.append(long_result)
        if short_result:
            results.append(short_result)

        return results

    # -----------------------------------------------------------------------
    # Per-direction update
    # -----------------------------------------------------------------------

    def _update_direction(
        self,
        state:     DirectionState,
        direction: str,
        on_side:   bool,    # True if bar_close is on the breakout side
        inside:    bool,    # True if bar_close is inside the window
        bar_close: float,
    ) -> RetestResult | None:
        """
        Advance one direction through its state machine.
        Returns a RetestResult if a notable event occurred, else None.
        """
        if state.entry_fired:
            return None

        # ---- Phase 1: waiting for breakout confirmation ----
        if not state.breakout_confirmed:
            if on_side:
                state.breakout_count += 1
                if state.breakout_count >= self.breakout_bars:
                    state.breakout_confirmed = True
                    state.breakout_extreme   = bar_close
                    if self._first_confirmed is None:
                        self._first_confirmed = direction
                    logger.info(
                        f"  {direction} BREAKOUT confirmed "
                        f"({self.breakout_bars} bars) @ {bar_close:.4f}"
                    )
                    return RetestResult(
                        direction   = direction,
                        entry_price = bar_close,
                        event       = "BREAKOUT",
                        range_high  = self._range_high,
                        range_low   = self._range_low,
                    )
                else:
                    logger.debug(
                        f"  {direction} breakout "
                        f"{state.breakout_count}/{self.breakout_bars} "
                        f"@ {bar_close:.4f}"
                    )
            else:
                state.breakout_count = 0
            return None

        # ---- Phase 2: waiting for retest credit ----
        if not state.retest_credited:
            if inside:
                state.retest_count += 1
                if state.retest_count >= self.retest_bars:
                    state.retest_credited = True
                    logger.info(
                        f"  {direction} RETEST confirmed "
                        f"({self.retest_bars} bars inside window) "
                        f"@ {bar_close:.4f}"
                    )
                    return RetestResult(
                        direction   = direction,
                        entry_price = bar_close,
                        event       = "RETEST",
                        range_high  = self._range_high,
                        range_low   = self._range_low,
                    )
                else:
                    logger.debug(
                        f"  {direction} retest "
                        f"{state.retest_count}/{self.retest_bars} "
                        f"@ {bar_close:.4f}"
                    )
            else:
                # Price moved away without completing retest — reset count
                # but keep breakout_confirmed (retest can happen later)
                if state.retest_count > 0:
                    logger.debug(
                        f"  {direction} retest reset "
                        f"(was {state.retest_count}/{self.retest_bars})"
                    )
                state.retest_count = 0
            return None

        # ---- Phase 3: waiting for reconfirmation (entry signal) ----
        if on_side:
            state.reconfirm_count += 1
            logger.debug(
                f"  {direction} reconfirm "
                f"{state.reconfirm_count}/{self.reconfirm_bars} "
                f"@ {bar_close:.4f}"
            )
            if state.reconfirm_count >= self.reconfirm_bars:
                state.entry_fired     = True
                state.reconfirm_count = 0
                logger.info(
                    f"  {direction} ENTRY "
                    f"({self.reconfirm_bars} bars reconfirmed) "
                    f"@ {bar_close:.4f}"
                )
                return RetestResult(
                    direction   = direction,
                    entry_price = bar_close,
                    event       = "ENTRY",
                    range_high  = self._range_high,
                    range_low   = self._range_low,
                )
        else:
            # Price moved off the breakout side — reset reconfirm count
            # but retest credit is NEVER revoked
            if state.reconfirm_count > 0:
                logger.debug(
                    f"  {direction} reconfirm reset "
                    f"(was {state.reconfirm_count}/{self.reconfirm_bars})"
                )
            state.reconfirm_count = 0

        return None
