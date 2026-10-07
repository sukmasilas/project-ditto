"""
Classic Trading API (XML, api.dll) integration for ricky.garage specifically.

This uses a SEPARATE, SHARED credential set (EBAY_TRADING_CLIENT_ID /
EBAY_TRADING_CLIENT_SECRET / EBAY_TRADING_REFRESH_TOKEN) that the unrelated
"Lister Tool" app on this same server also depends on for its own production
access to a real seller account with 316 live listings. This is a fallback
path for ricky.garage specifically because its REST-based token (EBAY_APP_ID/
EBAY_CERT_ID via ebay_auth.py) doesn't carry the scopes needed for the modern
Fulfillment/Message APIs - see claude.md.

HARD SAFETY GUARDRAILS - both are load-bearing, not just comments:

1. This module can only ever send the exact allowlisted read-only calls in
   ALLOWED_TRADING_CALLS. _call_trading_api() is the ONLY function in this
   file that actually performs the HTTP POST, and it raises (not asserts -
   asserts can be stripped with `python -O`) if the call name isn't on the
   list, before every single request. There is no other path to the Trading
   API here, and no generic "call anything" function is exposed.

2. EBAY_TRADING_REFRESH_TOKEN must NEVER be regenerated via a fresh
   authorization/consent flow. This module only ever exchanges the existing
   refresh token via grant_type=refresh_token. There is no authorization-code
   / consent-URL code path in this file, and there must never be one added -
   doing so would risk invalidating Lister Tool's production access to a real
   account with real live listings. See claude.md's gotcha on this - read it
   before touching this file.
"""
import base64
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

import requests

TRADING_API_URL = "https://api.ebay.com/ws/api.dll"
TRADING_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"

# Confirmed empirically against Lister Tool's own working production request
# headers on this server (same shared credential, read-only inspection, not
# guessed) - not from docs, since developer.ebay.com was unreachable for this
# investigation.
TRADING_SITE_ID = "0"
TRADING_COMPATIBILITY_LEVEL = "967"

EBAY_XML_NS = "urn:ebay:apis:eBLBaseComponents"

# Guardrail #1 (see module docstring). Every entry here must be read-only.
# GetNotificationPreferences was pre-allowlisted as a safe read-only option
# per the original task spec but isn't called by anything yet - no wrapper
# function exists for it. Its presence here doesn't grant any capability by
# itself, it only permits it if something calls it later.
#
# GetMyMessages (added 2026-08-21) is also strictly read-only (lists the
# account's own message inbox) - added because GetMemberMessages alone
# cannot determine whether a message genuinely still needs a reply (see
# fetch_trading_messages()'s docstring and claude.md's gotchas).
ALLOWED_TRADING_CALLS = frozenset({"GetOrders", "GetMemberMessages", "GetMyMessages", "GetNotificationPreferences"})


class TradingApiGuardrailError(Exception):
    pass


class TradingApiError(Exception):
    pass


_token_cache = {"access_token": None, "expires_at": None}


def get_trading_access_token(client_id, client_secret, refresh_token, force_refresh=False):
    """Mint (or reuse a cached) OAuth access token for the Trading credential.

    Never mints more than needed - reuses the cached token until close to its
    ~7200s expiry (60s safety buffer). Re-mints on expiry or when a caller
    signals a likely auth failure via force_refresh=True.

    ONLY ever exchanges the existing refresh_token (grant_type=refresh_token).
    There is deliberately no authorization-code path here - see guardrail #2
    in the module docstring and the claude.md gotcha.
    """
    now = datetime.now(timezone.utc)
    if (
        not force_refresh
        and _token_cache["access_token"] is not None
        and _token_cache["expires_at"] is not None
        and now < _token_cache["expires_at"]
    ):
        return _token_cache["access_token"]

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = requests.post(
        TRADING_TOKEN_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    response.raise_for_status()
    data = response.json()

    access_token = data["access_token"]
    expires_in = data.get("expires_in", 7200)
    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = now + timedelta(seconds=expires_in - 60)
    return access_token


def _strip_namespace(elem):
    """In-place strip the eBay XML namespace so callers can use plain tag
    names (find("Ack") instead of find("{urn:ebay:apis:eBLBaseComponents}Ack"))."""
    for el in elem.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return elem


def _find_text(elem, path):
    if elem is None:
        return None
    found = elem.find(path)
    return found.text if found is not None else None


def _extract_errors(root):
    errors = []
    for error_el in root.iter("Errors"):
        errors.append({
            "code": _find_text(error_el, "ErrorCode"),
            "message": _find_text(error_el, "LongMessage") or _find_text(error_el, "ShortMessage"),
        })
    return errors


def _looks_like_auth_failure(errors):
    for e in errors:
        text = f"{e.get('message', '')}".lower()
        if "token" in text and ("expired" in text or "invalid" in text):
            return True
    return False


def _call_trading_api(call_name, xml_body, access_token):
    # Guardrail #1 - see module docstring. Deliberately a raise, not an
    # assert, so this can't be stripped by running Python with -O.
    if call_name not in ALLOWED_TRADING_CALLS:
        raise TradingApiGuardrailError(
            f"Refused to send disallowed Trading API call {call_name!r}. "
            f"Only {sorted(ALLOWED_TRADING_CALLS)} are permitted - this credential "
            f"has full seller-account power over a real account with live listings."
        )

    headers = {
        "X-EBAY-API-CALL-NAME": call_name,
        "X-EBAY-API-SITEID": TRADING_SITE_ID,
        "X-EBAY-API-COMPATIBILITY-LEVEL": TRADING_COMPATIBILITY_LEVEL,
        "X-EBAY-API-IAF-TOKEN": access_token,
        "Content-Type": "text/xml",
    }
    response = requests.post(TRADING_API_URL, headers=headers, data=xml_body.encode("utf-8"))
    response.raise_for_status()
    return _strip_namespace(ET.fromstring(response.content))


def _call_with_auth_retry(call_name, xml_body, client_id, client_secret, refresh_token):
    access_token = get_trading_access_token(client_id, client_secret, refresh_token)
    root = _call_trading_api(call_name, xml_body, access_token)

    ack = _find_text(root, "Ack")
    if ack in ("Failure", "PartialFailure"):
        errors = _extract_errors(root)
        if _looks_like_auth_failure(errors):
            access_token = get_trading_access_token(client_id, client_secret, refresh_token, force_refresh=True)
            root = _call_trading_api(call_name, xml_body, access_token)
            ack = _find_text(root, "Ack")
            errors = _extract_errors(root)
        if ack == "Failure":
            raise TradingApiError(f"{call_name} failed: {errors}")

    return root


def _format_ebay_timestamp(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fetch_trading_orders(client_id, client_secret, refresh_token, window_start, window_end):
    """GetOrders, OrderStatus=Completed, for [window_start, window_end).

    Returns a list of dicts: order_id, buyer, title, total, currency.
    Caller is responsible for the rolling-window overlap and diffing by
    order_id against previously-seen state - this just fetches one window.
    """
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetOrdersRequest xmlns="{EBAY_XML_NS}">
  <CreateTimeFrom>{_format_ebay_timestamp(window_start)}</CreateTimeFrom>
  <CreateTimeTo>{_format_ebay_timestamp(window_end)}</CreateTimeTo>
  <OrderStatus>Completed</OrderStatus>
  <OrderRole>Seller</OrderRole>
  <Pagination>
    <EntriesPerPage>100</EntriesPerPage>
    <PageNumber>1</PageNumber>
  </Pagination>
</GetOrdersRequest>"""

    root = _call_with_auth_retry("GetOrders", xml_body, client_id, client_secret, refresh_token)

    orders = []
    for order_el in root.iter("Order"):
        total_el = order_el.find("Total")
        item_el = order_el.find("TransactionArray/Transaction/Item")
        orders.append({
            "order_id": _find_text(order_el, "OrderID"),
            "buyer": _find_text(order_el, "BuyerUserID") or "unknown buyer",
            "title": _find_text(item_el, "Title") if item_el is not None else None,
            "total": total_el.text if total_el is not None else None,
            "currency": total_el.attrib.get("currencyID", "") if total_el is not None else "",
        })
    return orders


def _fetch_member_message_content(client_id, client_secret, refresh_token, lookback_days):
    """GetMemberMessages, MailMessageType=All, no MessageStatus filter - this
    is a content lookup only (body/sender/item), not a status source. See
    fetch_trading_messages() for why MessageStatus itself is never trusted.

    No MessageStatus tag is sent at all: MessageStatus=All is confirmed
    invalid for this call (empirically, by the project owner), and querying
    Unanswered/Answered separately would still require trusting eBay's own
    (unreliable, see claude.md) status split. Omitting the tag entirely was
    confirmed empirically (2026-08-21) to return every message regardless of
    status in one call.

    MailMessageType=All, not AskSellerQuestion - AskSellerQuestion only
    catches per-listing "ask seller a question" messages and silently misses
    other legitimate buyer messages, e.g. ones sent through eBay's generic
    "Contact Member" flow (MessageType=ContactEbayMember, QuestionType=
    General, which also has no <Item> at all - confirmed empirically via a
    real message, MessageID 6400438617019 "Hello" from otodot). See
    claude.md's gotchas.

    Returns a dict keyed by message_id: {sender, body, item_id}. Field names
    (MemberMessageExchange > Question > {MessageID, SenderID, Body}, and
    Item/ItemID for the item, when present) were confirmed correct against
    real live responses on 2026-08-20/21 - see claude.md's gotchas.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)

    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMemberMessagesRequest xmlns="{EBAY_XML_NS}">
  <WarningLevel>High</WarningLevel>
  <MailMessageType>All</MailMessageType>
  <StartCreationTime>{_format_ebay_timestamp(start)}</StartCreationTime>
  <EndCreationTime>{_format_ebay_timestamp(now)}</EndCreationTime>
  <Pagination>
    <EntriesPerPage>200</EntriesPerPage>
    <PageNumber>1</PageNumber>
  </Pagination>
</GetMemberMessagesRequest>"""

    root = _call_with_auth_retry("GetMemberMessages", xml_body, client_id, client_secret, refresh_token)

    content = {}
    for exchange_el in root.iter("MemberMessageExchange"):
        question_el = exchange_el.find("Question")
        message_id = _find_text(question_el, "MessageID") or _find_text(exchange_el, "MessageID")
        if not message_id:
            continue
        content[message_id] = {
            "sender": _find_text(question_el, "SenderID") or "unknown buyer",
            "body": _find_text(question_el, "Body") or "",
            "item_id": _find_text(exchange_el, "Item/ItemID"),
        }
    return content


def _fetch_inbox_replied_status(client_id, client_secret, refresh_token, max_pages=25):
    """GetMyMessages, FolderID=0 (Inbox), paginated to cover the whole inbox.

    This is the reliable per-message ground truth this feature is built
    around: each Message here carries its own <Replied>true/false</Replied>
    flag and an <ExternalMessageID> matching GetMemberMessages' MessageID.
    Confirmed empirically (2026-08-21) that this is genuinely per-message,
    not thread-batched like GetMemberMessages' MessageStatus: two of
    chrifran-8088's messages (3468196798018, 3464758132018) show
    Replied=false here despite both being swept into MessageStatus=Answered
    by the same batch flip that hit 13 other unrelated messages at once. See
    claude.md's gotchas.

    No date range is sent (GetMyMessages doesn't take one) - the whole inbox
    is paginated instead, so there's no lookback window to silently age
    things out of. eBay's own system notices (Sender=eBay) have no
    ExternalMessageID and are skipped, since they're not buyer messages.

    Returns a dict keyed by message_id (ExternalMessageID): replied (bool).
    """
    replied_status = {}
    per_page = 200
    page = 1
    while page <= max_pages:
        xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyMessagesRequest xmlns="{EBAY_XML_NS}">
  <WarningLevel>High</WarningLevel>
  <FolderID>0</FolderID>
  <DetailLevel>ReturnHeaders</DetailLevel>
  <Pagination>
    <EntriesPerPage>{per_page}</EntriesPerPage>
    <PageNumber>{page}</PageNumber>
  </Pagination>
</GetMyMessagesRequest>"""
        root = _call_with_auth_retry("GetMyMessages", xml_body, client_id, client_secret, refresh_token)
        page_messages = root.findall(".//Message")
        for message_el in page_messages:
            external_id = _find_text(message_el, "ExternalMessageID")
            if not external_id:
                continue
            replied_status[external_id] = _find_text(message_el, "Replied") == "true"
        if len(page_messages) < per_page:
            break
        page += 1
    return replied_status


def fetch_trading_messages(client_id, client_secret, refresh_token, lookback_days=180):
    """Fetch buyer messages with a reliable, per-message "needs reply" signal.

    Deliberately does NOT trust GetMemberMessages' MessageStatus field to
    decide who's Answered/Unanswered - confirmed empirically (2026-08-21)
    that it's tracked at the conversation/thread level, not per message: one
    seller reply flipped 13 unrelated messages spanning 13 days to
    "Answered" at once, including some (per _fetch_inbox_replied_status's
    Replied flag) that were never actually addressed. See claude.md.

    The original design for this (mirroring ricky.game's
    process_customer_conversations, which compares the latest message's
    sender against the seller's own username) doesn't carry over directly:
    confirmed empirically that GetMemberMessages never exposes the seller's
    own outgoing replies as a message at all - every entry across a real
    90-day pull had SenderID = a buyer, never ricky.garage, and there's no
    "Sent"-equivalent in that call. GetMyMessages' per-message Replied flag
    (see _fetch_inbox_replied_status) is the closest real equivalent:
    reliable, per-message ground truth for "has this been addressed",
    computed by eBay itself rather than inferred from a sender comparison
    this API's shape doesn't support.

    lookback_days defaults to a generous 180 (eBay accepted up to 365 days
    in real testing with no errors, so this isn't API-mandated - it's just
    this code's own choice). This window only affects which messages'
    *content* (body/sender/item) gets fetched this poll, not whether a
    message is considered handled - the caller tracks seen message_ids in
    persistent state, so a message already recorded stays known even after
    its creation date ages past this window on a later poll.

    Returns a list of dicts: message_id, sender, body, item_id, replied.
    """
    content = _fetch_member_message_content(client_id, client_secret, refresh_token, lookback_days)
    replied_status = _fetch_inbox_replied_status(client_id, client_secret, refresh_token)

    messages = []
    for message_id, info in content.items():
        messages.append({
            "message_id": message_id,
            "sender": info["sender"],
            "body": info["body"],
            "item_id": info["item_id"],
            # Defaults to False (i.e. "still needs a reply") if GetMyMessages
            # somehow doesn't have a matching entry - safer to surface an
            # uncertain message than to silently swallow it.
            "replied": replied_status.get(message_id, False),
        })
    return messages
