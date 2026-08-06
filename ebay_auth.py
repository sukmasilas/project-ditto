import base64
import os

import requests
from dotenv import load_dotenv

load_dotenv()

APP_ID = os.environ["EBAY_APP_ID"]
CERT_ID = os.environ["EBAY_CERT_ID"]
RUNAME = os.environ["EBAY_RUNAME"]

AUTH_URL = "https://auth.ebay.com/oauth2/authorize"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SCOPE = (
    "https://api.ebay.com/oauth/api_scope/commerce.message "
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"
)


def _basic_auth_header():
    credentials = base64.b64encode(f"{APP_ID}:{CERT_ID}".encode()).decode()
    return f"Basic {credentials}"


def _post_token_request(data):
    response = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=data,
    )
    response.raise_for_status()
    return response.json()


def exchange_code_for_tokens(code):
    return _post_token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": RUNAME,
        }
    )


def get_access_token(refresh_token):
    return _post_token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": SCOPE,
        }
    )["access_token"]
