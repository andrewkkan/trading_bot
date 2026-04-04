"""
broker/etrade.py — E*TRADE API client.
Handles OAuth 1.0a authentication, equity and option order placement,
option chain polling, and order status checks.

Dependencies:
    pip install requests requests-oauthlib
"""

import uuid
import time
import webbrowser
from dataclasses import dataclass
from datetime import date
from requests_oauthlib import OAuth1Session
from utils.logger import get_logger

logger = get_logger(__name__)

BASE_URL_LIVE    = "https://api.etrade.com"
BASE_URL_SANDBOX = "https://apisb.etrade.com"


# ---------------------------------------------------------------------------
# Order dataclasses
# ---------------------------------------------------------------------------

@dataclass
class EquityOrder:
    """A simple equity (stock/ETF) order."""
    symbol: str
    side: str                        # "BUY" or "SELL"
    quantity: int
    order_type: str                  # "MARKET" or "LIMIT"
    limit_price: float | None = None
    reason: str = ""


@dataclass
class OptionOrder:
    """
    An option order.

    symbol       : underlying ticker, e.g. "QQQ"
    expiry       : datetime.date of expiration, e.g. date(2026, 4, 17)
    option_type  : "CALL" or "PUT"
    strike       : strike price as a float, e.g. 480.0
    side         : order action —
                     "BUY_OPEN"   buy to open  (long the option)
                     "SELL_CLOSE" sell to close (exit a long)
                     "SELL_OPEN"  sell to open  (write/short the option)
                     "BUY_CLOSE"  buy to close  (exit a short)
    quantity     : number of contracts (1 contract = 100 shares)
    order_type   : "MARKET" or "LIMIT"
    limit_price  : required when order_type is "LIMIT" (per-contract price)
    reason       : optional label for logging
    """
    symbol: str
    expiry: date
    option_type: str
    strike: float
    side: str
    quantity: int
    order_type: str
    limit_price: float | None = None
    reason: str = ""

    def to_etrade_symbol(self) -> str:
        """
        Build the E*TRADE option symbol string.
        Format: underlier:year:month:day:optionType:strikePrice
        Example: QQQ:2026:4:17:CALL:480.000000
        """
        return (
            f"{self.symbol}:{self.expiry.year}:{self.expiry.month}:"
            f"{self.expiry.day}:{self.option_type}:{self.strike:.6f}"
        )


# ---------------------------------------------------------------------------
# E*TRADE client
# ---------------------------------------------------------------------------

class ETradeClient:
    def __init__(self, consumer_key: str, consumer_secret: str, sandbox: bool = True):
        self.consumer_key    = consumer_key
        self.consumer_secret = consumer_secret
        self.base_url        = BASE_URL_SANDBOX if sandbox else BASE_URL_LIVE
        self.session: OAuth1Session | None = None
        self.account_id: str | None = None

    # -----------------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------------

    def authenticate(self):
        """
        Full OAuth 1.0a flow. Requires a one-time browser step per session.
        Saves an authenticated session for all subsequent API calls.
        """
        # Step 1 — request token
        oauth = OAuth1Session(self.consumer_key, client_secret=self.consumer_secret)
        resp  = oauth.fetch_request_token(f"{self.base_url}/oauth/request_token")
        owner_key    = resp.get("oauth_token")
        owner_secret = resp.get("oauth_token_secret")

        # Step 2 — user authorises in browser
        auth_url = (
            f"https://us.etrade.com/e/t/etws/authorize"
            f"?key={self.consumer_key}&token={owner_key}"
        )
        logger.info(f"Opening browser for E*TRADE authorization...")
        webbrowser.open(auth_url)
        verifier = input("Paste the verifier code from E*TRADE: ").strip()

        # Step 3 — exchange for access token
        oauth = OAuth1Session(
            self.consumer_key,
            client_secret=self.consumer_secret,
            resource_owner_key=owner_key,
            resource_owner_secret=owner_secret,
            verifier=verifier,
        )
        tokens = oauth.fetch_access_token(f"{self.base_url}/oauth/access_token")

        self.session = OAuth1Session(
            self.consumer_key,
            client_secret=self.consumer_secret,
            resource_owner_key=tokens["oauth_token"],
            resource_owner_secret=tokens["oauth_token_secret"],
        )

        logger.info("E*TRADE authentication successful.")
        self.account_id = self._fetch_account_id()

    def _fetch_account_id(self) -> str:
        """Return the accountIdKey of the first account on the profile."""
        url  = f"{self.base_url}/v1/accounts/list"
        resp = self.session.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        account_id = (
            resp.json()["AccountListResponse"]["Accounts"]["Account"][0]["accountIdKey"]
        )
        logger.info(f"Using account: {account_id}")
        return account_id

    # -----------------------------------------------------------------------
    # Equity orders
    # -----------------------------------------------------------------------

    def place_equity_order(self, order: EquityOrder) -> dict:
        """
        Place a market or limit equity (stock/ETF) order.

        Args:
            order: EquityOrder dataclass

        Returns:
            Raw JSON response from E*TRADE.
        """
        self._require_auth()

        payload = {
            "PlaceOrderRequest": {
                "orderType":     order.order_type,
                "clientOrderId": _new_order_id(),
                "Order": [{
                    "Instrument": [{
                        "Product": {
                            "securityType": "EQ",
                            "symbol":       order.symbol,
                        },
                        "orderAction":  order.side,
                        "quantityType": "QUANTITY",
                        "quantity":     order.quantity,
                    }],
                    "priceType":     order.order_type,
                    **({"limitPrice": order.limit_price} if order.limit_price else {}),
                    "orderTerm":     "GOOD_FOR_DAY",
                    "marketSession": "REGULAR",
                }],
            }
        }

        return self._post_order(payload)

    # keep old name as an alias so existing main.py calls still work
    def place_order(self, order: EquityOrder) -> dict:
        return self.place_equity_order(order)

    # -----------------------------------------------------------------------
    # Option orders
    # -----------------------------------------------------------------------

    def place_option_order(self, order: OptionOrder) -> dict:
        """
        Place a single-leg option order (buy to open, sell to close, etc.).

        Args:
            order: OptionOrder dataclass

        Returns:
            Raw JSON response from E*TRADE.

        Notes:
            - order.limit_price is per-contract (E*TRADE convention).
            - order.quantity is number of contracts (1 = 100 shares).
            - For MARKET orders on options, E*TRADE may reject during
              regular hours for illiquid strikes — prefer LIMIT.
        """
        self._require_auth()

        logger.info(
            f"Placing option order: {order.side} {order.quantity}x "
            f"{order.to_etrade_symbol()} @ "
            f"{'MARKET' if order.order_type == 'MARKET' else f'${order.limit_price:.2f}'}"
            f" | reason: {order.reason}"
        )

        payload = {
            "PlaceOrderRequest": {
                "orderType":     order.order_type,
                "clientOrderId": _new_order_id(),
                "Order": [{
                    "Instrument": [{
                        "Product": {
                            "securityType": "OPTN",
                            "symbol":       order.symbol,
                            "callPut":      order.option_type,      # "CALL" or "PUT"
                            "expiryYear":   order.expiry.year,
                            "expiryMonth":  order.expiry.month,
                            "expiryDay":    order.expiry.day,
                            "strikePrice":  order.strike,
                        },
                        "orderAction":  order.side,                 # BUY_OPEN etc.
                        "quantityType": "QUANTITY",
                        "quantity":     order.quantity,
                    }],
                    "priceType":     order.order_type,
                    **({"limitPrice": order.limit_price} if order.limit_price else {}),
                    "orderTerm":     "GOOD_FOR_DAY",
                    "marketSession": "REGULAR",
                }],
            }
        }

        return self._post_order(payload)

    def place_option_spread(
        self,
        buy_leg:  OptionOrder,
        sell_leg: OptionOrder,
        net_debit: float | None = None,
    ) -> dict:
        """
        Place a two-leg vertical spread as a single order.

        Both legs must share the same underlying, expiry, and option_type.
        net_debit is the total price you are willing to pay (debit spread)
        or receive (credit spread) for the spread. If None, order is MARKET.

        Example — QQQ bull call spread:
            buy_leg  = OptionOrder("QQQ", date(2026,4,17), "CALL", 480, "BUY_OPEN",  1, "LIMIT", 3.50)
            sell_leg = OptionOrder("QQQ", date(2026,4,17), "CALL", 485, "SELL_OPEN", 1, "LIMIT", 1.80)
            broker.place_option_spread(buy_leg, sell_leg, net_debit=1.70)
        """
        self._require_auth()

        order_type = "LIMIT" if net_debit is not None else "MARKET"

        logger.info(
            f"Placing spread: BUY {buy_leg.strike}{buy_leg.option_type} / "
            f"SELL {sell_leg.strike}{sell_leg.option_type} "
            f"exp {buy_leg.expiry} | net={'MARKET' if net_debit is None else f'${net_debit:.2f}'}"
        )

        def _instrument(leg: OptionOrder) -> dict:
            return {
                "Product": {
                    "securityType": "OPTN",
                    "symbol":       leg.symbol,
                    "callPut":      leg.option_type,
                    "expiryYear":   leg.expiry.year,
                    "expiryMonth":  leg.expiry.month,
                    "expiryDay":    leg.expiry.day,
                    "strikePrice":  leg.strike,
                },
                "orderAction":  leg.side,
                "quantityType": "QUANTITY",
                "quantity":     leg.quantity,
            }

        payload = {
            "PlaceOrderRequest": {
                "orderType":     order_type,
                "clientOrderId": _new_order_id(),
                "Order": [{
                    "Instrument": [_instrument(buy_leg), _instrument(sell_leg)],
                    "priceType":     order_type,
                    **({"limitPrice": net_debit} if net_debit is not None else {}),
                    "orderTerm":     "GOOD_FOR_DAY",
                    "marketSession": "REGULAR",
                }],
            }
        }

        return self._post_order(payload)

    # -----------------------------------------------------------------------
    # Option market data
    # -----------------------------------------------------------------------

    def get_option_chain(
        self,
        symbol: str,
        expiry: date,
        strike_near: float | None = None,
        num_strikes: int = 10,
        chain_type: str = "CALLPUT",        # "CALL", "PUT", or "CALLPUT"
        include_weekly: bool = True,
    ) -> dict:
        """
        Fetch the option chain for one expiry, filtered to strikes near
        the current price.

        Returns the raw JSON response. Each contract includes:
            bid, ask, bidSize, askSize, lastPrice, volume, openInterest,
            and a full OptionGreeks block (delta, gamma, theta, vega, rho, iv).

        Args:
            symbol       : underlying ticker, e.g. "QQQ"
            expiry       : expiration date
            strike_near  : anchor point for strike selection (usually current price)
            num_strikes  : number of strikes above+below the anchor
            chain_type   : "CALL", "PUT", or "CALLPUT"
            include_weekly: include weekly expirations

        Example:
            chain = broker.get_option_chain("QQQ", date(2026, 4, 17),
                                             strike_near=480.0, num_strikes=5)
        """
        self._require_auth()

        params = {
            "symbol":         symbol,
            "expiryYear":     expiry.year,
            "expiryMonth":    expiry.month,
            "expiryDay":      expiry.day,
            "chainType":      chain_type,
            "includeWeekly":  str(include_weekly).lower(),
            "skipAdjusted":   "true",
            "optionCategory": "STANDARD",
        }
        if strike_near is not None:
            params["strikePriceNear"] = strike_near
            params["noOfStrikes"]     = num_strikes

        resp = self.session.get(
            f"{self.base_url}/v1/market/optionchains",
            params=params,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()

    def get_option_expiry_dates(self, symbol: str) -> list[date]:
        """
        Return all available expiration dates for an underlying.
        Useful for discovering which expiries to target.
        """
        self._require_auth()

        resp = self.session.get(
            f"{self.base_url}/v1/market/optionexpiredate",
            params={"symbol": symbol},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()

        dates = []
        for entry in resp.json().get("OptionExpireDateResponse", {}).get("ExpirationDate", []):
            try:
                dates.append(date(entry["year"], entry["month"], entry["day"]))
            except (KeyError, ValueError):
                pass
        return sorted(dates)

    def poll_option_chain(
        self,
        symbol: str,
        expiry: date,
        strike_near: float,
        num_strikes: int = 10,
        interval_seconds: int = 5,
        callback=None,
    ):
        """
        Continuously poll the option chain and call callback(chain) on each update.
        Blocks forever — run in a thread or use asyncio if needed.

        Args:
            callback : callable that receives the parsed chain dict each poll.
                       If None, just logs the ATM bid/ask.

        Example:
            def on_chain(chain):
                # process chain, fire orders, etc.
                pass

            broker.poll_option_chain("QQQ", date(2026,4,17), 480.0,
                                     num_strikes=5, interval_seconds=5,
                                     callback=on_chain)
        """
        logger.info(
            f"Starting option chain poll: {symbol} exp={expiry} "
            f"near={strike_near} every {interval_seconds}s"
        )
        while True:
            try:
                chain = self.get_option_chain(symbol, expiry, strike_near, num_strikes)

                if callback:
                    callback(chain)
                else:
                    # Default: log the first call pair's mid price
                    pairs = (
                        chain.get("OptionChainResponse", {})
                             .get("OptionPair", [])
                    )
                    if pairs:
                        call = pairs[0].get("Call", {})
                        bid  = call.get("bid", "?")
                        ask  = call.get("ask", "?")
                        iv   = call.get("OptionGreeks", {}).get("iv", "?")
                        logger.info(f"{symbol} ATM call: bid={bid} ask={ask} IV={iv}")

            except Exception as e:
                logger.error(f"Option chain poll error: {e}")

            time.sleep(interval_seconds)

    # -----------------------------------------------------------------------
    # Order status
    # -----------------------------------------------------------------------

    def get_order_status(self, order_id: str) -> dict:
        """Poll the status of a previously placed order."""
        self._require_auth()
        resp = self.session.get(
            f"{self.base_url}/v1/accounts/{self.account_id}/orders",
            params={"orderId": order_id},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _require_auth(self):
        if not self.session or not self.account_id:
            raise RuntimeError("Not authenticated — call authenticate() first.")

    def _post_order(self, payload: dict) -> dict:
        url  = f"{self.base_url}/v1/accounts/{self.account_id}/orders/place"
        resp = self.session.post(
            url,
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        if not resp.ok:
            logger.error(f"Order failed [{resp.status_code}]: {resp.text}")
            resp.raise_for_status()
        result = resp.json()
        logger.info(f"Order accepted: {result}")
        return result


def _new_order_id() -> str:
    """Generate a unique 20-char client order ID as required by E*TRADE."""
    return uuid.uuid4().hex[:20]
