import sys
from urllib.parse import quote

import requests

from ebay_auth import APP_ID, AUTH_URL, RUNAME, SCOPE, exchange_code_for_tokens


def print_consent_url():
    url = (
        f"{AUTH_URL}"
        f"?client_id={quote(APP_ID)}"
        f"&redirect_uri={quote(RUNAME)}"
        f"&response_type=code"
        f"&scope={quote(SCOPE)}"
    )
    print("Open this URL in a browser and sign in to authorize the app:\n")
    print(url)


def exchange_code_for_token(code):
    try:
        tokens = exchange_code_for_tokens(code)
    except requests.HTTPError as e:
        print(f"Token request failed ({e.response.status_code}):")
        print(e.response.text)
        sys.exit(1)

    print("Access token:\n")
    print(tokens.get("access_token"))
    print("\nRefresh token:\n")
    print(tokens.get("refresh_token"))


if __name__ == "__main__":
    if len(sys.argv) == 1:
        print_consent_url()
    else:
        exchange_code_for_token(sys.argv[1])
