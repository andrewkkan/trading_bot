"""
broker/etrade.py — E*TRADE API client.
Handles OAuth 1.0a authentication and order placement.

Dependencies:
    pip install requests requests-oauthlib
"""

import webbrowser
import requests
from requests_oauthlib import OAuth1Session
from utils.logger import get_logger

logger = get_logger(__name__)

BASE_URL_LIVE    = "https://api.etrade.com"
BASE_URL_SANDBOX = "https://apisb.etrade.com"


class ETradeClient:
    def __init__(self, consumer_key: str, consumer_secret: str, sandbox: bool = True):
        self.consumer_key    = consumer_key
        self.consumer_secret = consumer_secret
        self.base_url        = BASE_URL_SANDBOX if sandbox else BASE_URL_LIVE
        self.session: OAuth1Session | None = None
        self.account_id: str | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self):
        """
        Full OAuth 1.0a flow. Requires a one-time browser step to authorize.
        Saves an authenticated session for subsequent API calls.
        """
        # Step 1 — get a request token
        oauth = OAuth1Session(self.consumer_key, client_secret=self.consumer_secret)
        fetch_response = oauth.fetch_request_token(
            f"{self.base_url}/oauth/request_token"
        )
        resource_owner_key    = fetch_response.get("oauth_token")
        resource_owner_secret = fetch_response.get("oauth_token_secret")

        # Step 2 — send user to E*TRADE to authorize
        auth_url = (
            f"https://us.etrade.com/e/t/etws/authorize"
            f"?key={self.consumer_key}&token={resource_owner_key}"
        )
        logger.info(f"Opening browser for authorization: {auth_url}")
        webbrowser.open(auth_url)

        verifier = input("Paste the verifier code from E*TRADE here: ").strip()

        # Step 3 — exchange for an access token
        oauth = OAuth1Session(
            self.consumer_key,
            client_secret=self.consumer_secret,
            resource_owner_key=resource_owner_key,
            resource_owner_secret=resource_owner_secret,
            verifier=verifier,
        )
        access_token_response = oauth.fetch_access_token(
            f"{self.base_url}/oauth/access_token"
        )

        self.session = OAuth1Session(
            self.consumer_key,
            client_secret=self.consumer_secret,
            resource_owner_key=access_token_response["oauth_token"],
            resource_owner_secret=access_token_response["oauth_token_secret"],
        )

        logger.info("E*TRADE authentication successful.")
        self.account_id = self._fetch_account_id()

    def _fetch_account_id(self) -> str:
        """Retrieve the first account ID on the authenticated account."""
        url = f"{self.base_url}/v1/accounts/list"
        resp = self.session.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        accounts = resp.json()["AccountListResponse"]["Accounts"]["Account"]
        account_id = accounts[0]["accountIdKey"]
        logger.info(f"Using account ID: {account_id}")
        return account_id

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_order(self, order) -> dict:
        """
        Place a market or limit order via E*TRADE.

        Args:
            order: An Order dataclass from strategy/signals.py

        Returns:
            The raw JSON response from E*TRADE.
        """
        if not self.session or not self.account_id:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        url = f"{self.base_url}/v1/accounts/{self.account_id}/orders/place"

        payload = {
            "PlaceOrderRequest": {
                "orderType": order.order_type,
                "clientOrderId": self._generate_client_order_id(),
                "Order": [
                    {
                        "Instrument": [
                            {
                                "Product": {
                                    "securityType": "EQ",
                                    "symbol": order.symbol,
                                },
                                "orderAction": order.side,
                                "quantityType": "QUANTITY",
                                "quantity": order.quantity,
                            }
                        ],
                        "priceType": order.order_type,
                        **({"limitPrice": order.limit_price} if order.limit_price else {}),
                        "orderTerm": "GOOD_FOR_DAY",
                        "marketSession": "REGULAR",
                    }
                ],
            }
        }

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

    def get_order_status(self, order_id: str) -> dict:
        """Poll the status of a previously placed order."""
        url = f"{self.base_url}/v1/accounts/{self.account_id}/orders"
        resp = self.session.get(
            url,
            params={"orderId": order_id},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _generate_client_order_id() -> str:
        """E*TRADE requires a unique client-side order ID per request."""
        import uuid
        return str(uuid.uuid4()).replace("-", "")[:20]
