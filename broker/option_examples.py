"""
broker/option_examples.py — Runnable examples for every option order type.

Run from the project root after authenticating:
    python -m broker.option_examples
"""

from datetime import date
from broker.etrade import ETradeClient, OptionOrder
from config import Config


def main():
    broker = ETradeClient(
        consumer_key=Config.ETRADE_CONSUMER_KEY,
        consumer_secret=Config.ETRADE_CONSUMER_SECRET,
        sandbox=Config.SANDBOX_MODE,        # True = sandbox, no real money
    )
    broker.authenticate()

    # -----------------------------------------------------------------------
    # 1. Discover available expiration dates
    # -----------------------------------------------------------------------
    print("\n--- Available QQQ expirations ---")
    expiries = broker.get_option_expiry_dates("QQQ")
    for d in expiries[:6]:               # print the first 6
        print(f"  {d}")
    next_expiry = expiries[0]            # use the nearest expiry for examples

    # -----------------------------------------------------------------------
    # 2. Fetch the option chain (bid/ask/greeks for all strikes near 480)
    # -----------------------------------------------------------------------
    print("\n--- QQQ option chain (ATM ±5 strikes) ---")
    chain = broker.get_option_chain(
        symbol="QQQ",
        expiry=next_expiry,
        strike_near=480.0,
        num_strikes=10,
        chain_type="CALLPUT",
    )
    pairs = chain.get("OptionChainResponse", {}).get("OptionPair", [])
    for pair in pairs:
        call = pair.get("Call", {})
        put  = pair.get("Put",  {})
        strike = call.get("strikePrice", "?")
        print(
            f"  Strike {strike:>7} | "
            f"CALL bid={call.get('bid','?'):>6}  ask={call.get('ask','?'):>6}  "
            f"delta={call.get('OptionGreeks',{}).get('delta','?'):>7}  "
            f"IV={call.get('OptionGreeks',{}).get('iv','?'):>6} | "
            f"PUT  bid={put.get('bid','?'):>6}  ask={put.get('ask','?'):>6}"
        )

    # -----------------------------------------------------------------------
    # 3. Buy to open a single call (long call)
    # -----------------------------------------------------------------------
    print("\n--- Buy to open: 1x QQQ 480 Call ---")
    result = broker.place_option_order(OptionOrder(
        symbol      = "QQQ",
        expiry      = date(2026, 4, 17),
        option_type = "CALL",
        strike      = 480.0,
        side        = "BUY_OPEN",
        quantity    = 1,
        order_type  = "LIMIT",
        limit_price = 3.50,             # pay no more than $3.50/contract
        reason      = "ORB bullish breakout",
    ))
    print(f"  Response: {result}")

    # -----------------------------------------------------------------------
    # 4. Sell to close that call (exit the long)
    # -----------------------------------------------------------------------
    print("\n--- Sell to close: 1x QQQ 480 Call ---")
    result = broker.place_option_order(OptionOrder(
        symbol      = "QQQ",
        expiry      = date(2026, 4, 17),
        option_type = "CALL",
        strike      = 480.0,
        side        = "SELL_CLOSE",
        quantity    = 1,
        order_type  = "LIMIT",
        limit_price = 4.20,             # target exit price
        reason      = "Take profit",
    ))
    print(f"  Response: {result}")

    # -----------------------------------------------------------------------
    # 5. Buy a put (protective put / bearish directional)
    # -----------------------------------------------------------------------
    print("\n--- Buy to open: 1x QQQ 475 Put ---")
    result = broker.place_option_order(OptionOrder(
        symbol      = "QQQ",
        expiry      = date(2026, 4, 17),
        option_type = "PUT",
        strike      = 475.0,
        side        = "BUY_OPEN",
        quantity    = 1,
        order_type  = "LIMIT",
        limit_price = 2.80,
        reason      = "ORB bearish breakout",
    ))
    print(f"  Response: {result}")

    # -----------------------------------------------------------------------
    # 6. Bull call spread (two legs as a single order)
    #    Buy 480 Call / Sell 485 Call — net debit ~$1.70
    # -----------------------------------------------------------------------
    print("\n--- Bull call spread: BUY 480C / SELL 485C ---")
    result = broker.place_option_spread(
        buy_leg = OptionOrder(
            symbol="QQQ", expiry=date(2026, 4, 17),
            option_type="CALL", strike=480.0,
            side="BUY_OPEN", quantity=1, order_type="LIMIT", limit_price=3.50,
        ),
        sell_leg = OptionOrder(
            symbol="QQQ", expiry=date(2026, 4, 17),
            option_type="CALL", strike=485.0,
            side="SELL_OPEN", quantity=1, order_type="LIMIT", limit_price=1.80,
        ),
        net_debit=1.70,               # max you're willing to pay for the spread
    )
    print(f"  Response: {result}")

    # -----------------------------------------------------------------------
    # 7. Market order (use sparingly — risky on wide-spread options)
    # -----------------------------------------------------------------------
    print("\n--- Market order: 1x QQQ 480 Call (use with care) ---")
    result = broker.place_option_order(OptionOrder(
        symbol      = "QQQ",
        expiry      = date(2026, 4, 17),
        option_type = "CALL",
        strike      = 480.0,
        side        = "BUY_OPEN",
        quantity    = 1,
        order_type  = "MARKET",        # no limit_price needed
        reason      = "Urgent fill",
    ))
    print(f"  Response: {result}")


if __name__ == "__main__":
    main()
