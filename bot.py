import asyncio
import os
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import dateparser
import discord
import requests
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from ebay_auth import get_access_token
import ebay_trading

load_dotenv()

BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
TIMEZONE_NAME = os.environ.get("BOT_TIMEZONE", "Asia/Jakarta")
LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)

DATA_FILE = "reminders.json"

def load_ebay_accounts():
    """Numbered-account config: EBAY_ACCOUNT_1_NAME, EBAY_ACCOUNT_1_REFRESH_TOKEN, etc.

    Preferred over a single JSON-blob env var since these get hand-edited via
    nano - numbered vars are much harder to typo into a broken file than JSON.
    Scans sequentially by NAME presence; an account missing REFRESH_TOKEN or
    ALERT_CHANNEL_ID is skipped (with a startup print) rather than attempted
    and failing every poll, same spirit as the old single-account guard.
    """
    accounts = []
    i = 1
    while True:
        prefix = f"EBAY_ACCOUNT_{i}_"
        name = os.environ.get(f"{prefix}NAME")
        if not name:
            break

        refresh_token = os.environ.get(f"{prefix}REFRESH_TOKEN")
        alert_channel_id_raw = os.environ.get(f"{prefix}ALERT_CHANNEL_ID")

        if refresh_token and alert_channel_id_raw:
            accounts.append({
                "name": name,
                "refresh_token": refresh_token,
                "seller_username": os.environ.get(f"{prefix}SELLER_USERNAME"),
                "alert_channel_id": int(alert_channel_id_raw),
            })
        else:
            print(f"eBay account '{name}' (EBAY_ACCOUNT_{i}_*) skipped: needs REFRESH_TOKEN and ALERT_CHANNEL_ID to be active.")

        i += 1
    return accounts


EBAY_ACCOUNTS = load_ebay_accounts()


# Classic Trading API (XML) fallback path, ricky.garage-specific - see
# ebay_trading.py's module docstring and claude.md for the full story and
# the two hard safety guardrails around this credential set. This is
# DELIBERATELY separate from EBAY_APP_ID/EBAY_CERT_ID (the REST credential
# set) - a different, shared credential that Lister Tool also depends on.
EBAY_TRADING_CLIENT_ID = os.environ.get("EBAY_TRADING_CLIENT_ID")
EBAY_TRADING_CLIENT_SECRET = os.environ.get("EBAY_TRADING_CLIENT_SECRET")
EBAY_TRADING_REFRESH_TOKEN = os.environ.get("EBAY_TRADING_REFRESH_TOKEN")
EBAY_TRADING_ACCOUNT_NAME = os.environ.get("EBAY_TRADING_ACCOUNT_NAME")
EBAY_TRADING_ENABLED = bool(
    EBAY_TRADING_CLIENT_ID and EBAY_TRADING_CLIENT_SECRET
    and EBAY_TRADING_REFRESH_TOKEN and EBAY_TRADING_ACCOUNT_NAME
)
# Reuses the matching EBAY_ACCOUNT_N_ALERT_CHANNEL_ID rather than a separate
# channel env var, since this posts into the same channel as that account's
# REST-based general feed - one channel ID to keep in sync, not two.
EBAY_TRADING_STATE_KEY = f"{EBAY_TRADING_ACCOUNT_NAME}_trading" if EBAY_TRADING_ACCOUNT_NAME else None


def is_trading_fallback_account(account):
    """True for the account the Trading API fallback covers. The REST-based
    check_ebay_activity skips it entirely: its REST token never worked
    (invalid_grant, then invalid_scope - see claude.md), so polling it there
    only produced a failed token refresh every cycle. The account entry
    itself still has to exist - the Trading loop reuses its ALERT_CHANNEL_ID.
    """
    return EBAY_TRADING_ENABLED and account["name"] == EBAY_TRADING_ACCOUNT_NAME
# How often the Trading API fallback polls. Defaults to 10 minutes (was a
# hardcoded 2): every poll is 3+ Trading API calls on the app shared with
# Lister Tool, and eBay was returning error 518 ("exceeded usage limit on
# this call") at the 2-minute rate.
EBAY_TRADING_POLL_MINUTES = max(1, int(os.environ.get("EBAY_TRADING_POLL_MINUTES") or "10"))


EBAY_ORDER_URL = "https://api.ebay.com/sell/fulfillment/v1/order"
EBAY_CONVERSATION_URL = "https://api.ebay.com/commerce/message/v1/conversation"
EBAY_STATE_FILE = "ebay_state.json"
EBAY_RATE_LIMIT_MESSAGE = "⚠️ eBay API rate limit reached — skipping this check, will retry next cycle"
EBAY_UNREPLIED_REMINDER_DELAY = timedelta(hours=24)
# How often the REST-based check_ebay_activity polls (orders, messages,
# 24h reminders). Defaults to 10 minutes (was a hardcoded 2) to cut eBay
# call volume ~5x. Safe for orders: each poll diffs the latest 50 by ID, no
# time window.
EBAY_POLL_MINUTES = max(1, int(os.environ.get("EBAY_POLL_MINUTES") or "10"))
# (connect, read) seconds - requests has no default timeout.
EBAY_HTTP_TIMEOUT = (10, 30)

# Every eBay HTTP call runs via asyncio.to_thread so a slow eBay response
# can't block the event loop (it used to, which starved discord.py's gateway
# heartbeat and made Discord drop the connection - hundreds of "heartbeat
# blocked" warnings a week). The catch: with the loop no longer frozen, the
# two eBay polling loops can now genuinely interleave,
# and each one loads, modifies and rewrites the WHOLE ebay_state.json - so
# one could save a stale copy over another's changes (lost seen-IDs ->
# duplicate notifications). Everything that load/modify/saves that file
# holds this lock for the whole read-modify-write.
ebay_state_lock = asyncio.Lock()

# Deliberately a single shared flag, not per-account: eBay's call limits are
# scoped per APPLICATION by default (shared across every token/user of that
# app) unless the app is specifically certified for per-user limiting, which
# is for large third-party apps with many external customers - not this one.
# Both accounts here share one EBAY_APP_ID/EBAY_CERT_ID, so they draw from
# the same quota; if one account gets limited the other is about to be too.
# See claude.md gotchas for the verification (docs + a blocked getRateLimits
# scope check consistent with the per-app model).
ebay_rate_limited = False


class EbayRateLimitError(Exception):
    pass


intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def load_reminders():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return []


def save_reminders(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


reminders = load_reminders()
next_id = max([r["id"] for r in reminders], default=0) + 1


@tree.command(name="remind", description="Set a reminder using natural language for when")
@app_commands.describe(
    when="When to remind you, e.g. 'tomorrow at 3pm', 'in 2 hours', 'Aug 10 2pm'",
    message="What you want to be reminded about",
)
async def remind(interaction: discord.Interaction, when: str, message: str):
    global next_id

    parsed_dt = dateparser.parse(
        when,
        settings={
            "TIMEZONE": TIMEZONE_NAME,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        },
    )

    if parsed_dt is None:
        await interaction.response.send_message(
            f"Couldn't understand \"{when}\" as a date/time. Try rephrasing, e.g. "
            "\"tomorrow at 3pm\", \"in 2 hours\", or \"Aug 10 2pm\".",
            ephemeral=True,
        )
        return

    utc_dt = parsed_dt.astimezone(timezone.utc)

    if utc_dt <= datetime.now(timezone.utc):
        await interaction.response.send_message(
            "That date/time is in the past. Pick a future date/time.", ephemeral=True
        )
        return

    reminder = {
        "id": next_id,
        "user_id": interaction.user.id,
        "channel_id": interaction.channel_id,
        "message": message,
        "remind_at_utc": utc_dt.isoformat(),
    }
    reminders.append(reminder)
    save_reminders(reminders)
    next_id += 1

    local_dt = utc_dt.astimezone(LOCAL_TZ)
    await interaction.response.send_message(
        f"Got it. I'll remind you about **{message}** on {local_dt.strftime('%Y-%m-%d %H:%M')} "
        f"({TIMEZONE_NAME}). (Reminder #{reminder['id']})"
    )


@tree.command(name="reminders", description="List your upcoming reminders")
async def list_reminders(interaction: discord.Interaction):
    mine = [r for r in reminders if r["user_id"] == interaction.user.id]
    if not mine:
        await interaction.response.send_message("You have no upcoming reminders.", ephemeral=True)
        return

    lines = []
    for r in sorted(mine, key=lambda x: x["remind_at_utc"]):
        local_time = datetime.fromisoformat(r["remind_at_utc"]).astimezone(LOCAL_TZ)
        lines.append(f"#{r['id']} — {local_time.strftime('%Y-%m-%d %H:%M')} — {r['message']}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="cancelreminder", description="Cancel a reminder by its ID")
@app_commands.describe(reminder_id="The ID number of the reminder to cancel (see /reminders)")
async def cancel_reminder(interaction: discord.Interaction, reminder_id: int):
    global reminders

    match = next(
        (r for r in reminders if r["id"] == reminder_id and r["user_id"] == interaction.user.id),
        None,
    )
    if not match:
        await interaction.response.send_message(
            "Couldn't find a reminder with that ID under your name.", ephemeral=True
        )
        return

    reminders = [r for r in reminders if r["id"] != reminder_id]
    save_reminders(reminders)
    await interaction.response.send_message(f"Cancelled reminder #{reminder_id}.", ephemeral=True)


@tasks.loop(seconds=30)
async def check_reminders():
    now = datetime.now(timezone.utc)
    due = [r for r in reminders if datetime.fromisoformat(r["remind_at_utc"]) <= now]

    for r in due:
        channel = client.get_channel(r["channel_id"])
        if channel is not None:
            try:
                await channel.send(f"⏰ <@{r['user_id']}> reminder: **{r['message']}**")
            except discord.HTTPException:
                pass
        reminders.remove(r)

    if due:
        save_reminders(reminders)


def load_ebay_state():
    """Top-level keys are account names (EBAY_ACCOUNT_*_NAME); everything that
    used to be flat top-level state now lives namespaced under state[name].
    Existing entries from before multi-account support were migrated once via
    a standalone script (not checked into the repo - see claude.md's gotcha
    on live state-file migrations for what that involved and what to do
    differently next time), not handled here.
    """
    if os.path.exists(EBAY_STATE_FILE):
        with open(EBAY_STATE_FILE, "r") as f:
            state = json.load(f)
    else:
        state = {}

    for account in EBAY_ACCOUNTS:
        if account["name"] not in state:
            # Brand-new account: its first poll records the existing orders
            # and messages as seen instead of announcing up to 50 old orders
            # and every unread conversation as "new".
            state[account["name"]] = {"silent_first_poll": True}
        account_state = state[account["name"]]
        account_state.setdefault("seen_order_ids", [])
        account_state.setdefault("seen_conversation_message_ids", {})
        # 24h unreplied reminders (check_unreplied_conversations). The old
        # #customer-service keys (customer_conversations etc.) are left in the
        # file untouched - read once to carry reminder state over, then unused.
        account_state.setdefault("pending_replies", {})

    if EBAY_TRADING_ENABLED:
        # Deliberately its own namespace, not merged into the REST-based
        # account_state above - different API, different ID space (eBay
        # Trading API OrderID/MessageID vs the REST orderId/messageId), kept
        # separate so the two dedup mechanisms can't be confused.
        trading_state = state.setdefault(EBAY_TRADING_STATE_KEY, {})
        trading_state.setdefault("seen_order_ids", [])
        trading_state.setdefault("seen_message_ids", [])
        trading_state.setdefault("pending_replies", {})

    return state


def save_ebay_state(state):
    with open(EBAY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_ebay_rate_limit_response(response):
    if response.status_code == 429:
        return True

    try:
        body = response.json()
    except ValueError:
        return False

    for err in body.get("errors", []) or []:
        message = f"{err.get('message', '')} {err.get('longMessage', '')}".lower()
        if "rate limit" in message or "call limit" in message or "too many requests" in message:
            return True

    return False


def fetch_ebay_orders(access_token):
    response = requests.get(
        EBAY_ORDER_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=EBAY_HTTP_TIMEOUT,
        params={"limit": 50},
    )
    if is_ebay_rate_limit_response(response):
        raise EbayRateLimitError("eBay order API rate limit reached")
    response.raise_for_status()
    return response.json().get("orders", [])


def fetch_ebay_conversations(access_token):
    response = requests.get(
        EBAY_CONVERSATION_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=EBAY_HTTP_TIMEOUT,
    )
    if is_ebay_rate_limit_response(response):
        raise EbayRateLimitError("eBay conversation API rate limit reached")
    response.raise_for_status()
    return response.json().get("conversations", [])


def fetch_recent_conversation_messages(access_token, convo_id, limit=25):
    """The newest `limit` messages of one conversation, oldest-first.

    A single API call. getConversation returns messages newest-first -
    confirmed empirically against the live API - reversed here.
    """
    response = requests.get(
        f"{EBAY_CONVERSATION_URL}/{convo_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=EBAY_HTTP_TIMEOUT,
        params={"conversation_type": "FROM_MEMBERS", "offset": 0, "limit": limit},
    )
    if is_ebay_rate_limit_response(response):
        raise EbayRateLimitError("eBay conversation history API rate limit reached")
    response.raise_for_status()
    messages = response.json().get("messages", [])
    messages.reverse()
    return messages


async def notify_ebay_rate_limit(channel):
    global ebay_rate_limited
    if ebay_rate_limited:
        return
    ebay_rate_limited = True
    try:
        await channel.send(EBAY_RATE_LIMIT_MESSAGE)
    except discord.HTTPException:
        pass


TRUNCATION_SUFFIX = "… (truncated)"


def truncate_for_discord(text, limit=1800):
    # Idempotent: some paths truncate a body and later pass it through here
    # again.
    if len(text) > limit and not (text.endswith(TRUNCATION_SUFFIX) and len(text) <= limit + len(TRUNCATION_SUFFIX)):
        return text[:limit].rstrip() + TRUNCATION_SUFFIX
    return text


def ebay_message_link(message_id):
    # eBay's Message Center deep link takes the latest message's messageId as
    # question_id - not the conversationId (see claude.md).
    if not message_id:
        return None
    return f"https://www.ebay.com/cnt/viewMessage?group_type=CORE&question_id={message_id}"


def build_unreplied_reminder_text(seller_label, buyer, message_body, message_count=1):
    # @everyone goes in content, not an embed - embeds never ping (see claude.md).
    what = "customer message" if message_count == 1 else f"{message_count} customer messages"
    latest = "" if message_count == 1 else "latest "
    return (
        f"@everyone ⏰ Still unreplied after 24h — **{seller_label}**, {what} from {buyer}:\n"
        f'{latest}"{message_body}"'
    )


def build_reply_view(message_link):
    if not message_link:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Reply on eBay", style=discord.ButtonStyle.link, url=message_link))
    return view


async def check_unreplied_conversations(channel, conversations, account_state, state, seller_username, access_token):
    """24h "still unreplied" reminders for a REST account, posted into its own
    alert channel (replaced the #customer-service threads, 2026-11-04).

    A conversation is waiting on a reply while its latest message is from
    someone other than the seller - read status doesn't matter (see
    claude.md). One reminder per waiting message: a new buyer message
    restarts the 24h clock, a seller reply clears it.

    pending_replies: {convo_id: {buyer, message_id, body, since_utc, reminded}}
    """
    now = datetime.now(timezone.utc)
    pending = account_state["pending_replies"]
    initializing = not account_state.get("reply_tracking_initialized")
    if initializing:
        # Carry the old customer-channel tracking over so its reminder clocks
        # and "already reminded" flags survive the switch. Conversations it had
        # resolved or that were marked Finished aren't in there, and nor is
        # anything on an account that never had that feature - those are
        # recorded below as already reminded, so switching this on doesn't
        # ping for every old unanswered conversation at once.
        for convo_id, entry in (account_state.get("customer_conversations") or {}).items():
            message_id = entry.get("last_ebay_message_id") or entry.get("message_id")
            if not message_id or not entry.get("first_seen_utc"):
                continue
            pending[convo_id] = {
                "buyer": entry.get("buyer_username"),
                "message_id": message_id,
                "body": "",
                "since_utc": entry["first_seen_utc"],
                "reminded": bool(entry.get("reminded")),
            }

    for convo in conversations:
        if convo.get("conversationType") != "FROM_MEMBERS":
            continue
        convo_id = convo.get("conversationId")
        latest_message = convo.get("latestMessage") or {}
        sender = latest_message.get("senderUsername")
        message_id = latest_message.get("messageId")
        if not convo_id or not message_id:
            continue

        entry = pending.get(convo_id)
        if sender == seller_username:
            if entry is not None:
                del pending[convo_id]
            continue

        if entry is None or entry["message_id"] != message_id:
            pending[convo_id] = {
                "buyer": sender or "unknown buyer",
                "message_id": message_id,
                "body": truncate_for_discord(latest_message.get("messageBody", ""), limit=300),
                "since_utc": now.isoformat(),
                # A carried-over conversation with a newer message is a real
                # new message, so only brand-new entries are silenced.
                "reminded": initializing and entry is None,
            }
            continue

        if not entry.get("body"):
            entry["body"] = truncate_for_discord(latest_message.get("messageBody", ""), limit=300)
        if entry["reminded"] or now - datetime.fromisoformat(entry["since_utc"]) < EBAY_UNREPLIED_REMINDER_DELAY:
            continue

        # eBay's conversation list can lag behind - latestMessage still showing
        # the buyer after the seller has replied. Check the conversation itself
        # before pinging everyone; one call, only when a reminder is due.
        try:
            newest = await asyncio.to_thread(fetch_recent_conversation_messages, access_token, convo_id, 1)
        except (EbayRateLimitError, requests.RequestException) as e:
            print(f"Couldn't double-check conversation {convo_id} before its 24h reminder, retrying next poll: {e}")
            continue
        if newest and newest[-1].get("senderUsername") == seller_username:
            # Already replied - stay quiet. Kept (not deleted) so the stale
            # list entry isn't re-added as new; it clears once the list
            # catches up and shows the seller's reply.
            entry["reminded"] = True
            save_ebay_state(state)
            continue

        try:
            await channel.send(
                build_unreplied_reminder_text(seller_username, entry["buyer"], entry["body"]),
                view=build_reply_view(ebay_message_link(message_id)),
            )
        except discord.HTTPException as e:
            print(f"Failed to send 24h reminder for conversation {convo_id}: {e}")
            continue
        entry["reminded"] = True
        save_ebay_state(state)

    account_state["reply_tracking_initialized"] = True
    save_ebay_state(state)


async def check_unreplied_trading_messages(channel, messages, trading_state, state, seller_label):
    """24h "still unreplied" reminders for the Trading API account.

    Uses GetMyMessages' per-message Replied flag (already fetched every poll -
    no extra calls). A reply to a buyer's newer message counts as answering
    their older ones too, the same way a seller reply clears a REST
    conversation. Reminders go out per buyer, once their newest unreplied
    message is 24h old, then that buyer's waiting messages are cleared.

    pending_replies: {message_id: {sender, body, since_utc}} - only messages
    first seen since this feature went live (2026-11-04), so the switch
    doesn't ping for months of old unanswered messages.
    """
    now = datetime.now(timezone.utc)
    pending = trading_state["pending_replies"]
    by_id = {m.get("message_id"): m for m in messages}

    def clear_sender(sender, up_to_since):
        for pid in [pid for pid, p in pending.items() if p["sender"] == sender and p["since_utc"] <= up_to_since]:
            del pending[pid]

    for pid, p in list(pending.items()):
        if pid not in pending:
            continue
        m = by_id.get(pid)
        if m is None:
            # Aged out of the fetch window - nothing left to check against.
            del pending[pid]
        elif m.get("replied"):
            clear_sender(p["sender"], p["since_utc"])

    by_sender = {}
    for pid, p in pending.items():
        by_sender.setdefault(p["sender"], []).append((pid, p))
    for sender, items in by_sender.items():
        items.sort(key=lambda item: item[1]["since_utc"])
        newest = items[-1][1]
        if now - datetime.fromisoformat(newest["since_utc"]) < EBAY_UNREPLIED_REMINDER_DELAY:
            continue
        try:
            await channel.send(build_unreplied_reminder_text(seller_label, sender, newest["body"], len(items)))
        except discord.HTTPException as e:
            print(f"Failed to send 24h reminder for Trading API messages from {sender}: {e}")
            continue
        for pid, _ in items:
            del pending[pid]

    save_ebay_state(state)


@tasks.loop(minutes=EBAY_POLL_MINUTES)
async def check_ebay_activity():
    async with ebay_state_lock:
        await _check_ebay_activity()


async def _check_ebay_activity():
    global ebay_rate_limited

    if not EBAY_ACCOUNTS:
        return

    state = load_ebay_state()

    for account in EBAY_ACCOUNTS:
        account_name = account["name"]

        if is_trading_fallback_account(account):
            # Its REST token has never worked (see claude.md) - attempting it
            # just logged a failed token refresh every poll, ~700/day.
            continue

        channel = client.get_channel(account["alert_channel_id"])
        if channel is None:
            print(f"eBay alert channel {account['alert_channel_id']} not found for account {account_name}.")
            continue

        try:
            access_token = await asyncio.to_thread(get_access_token, account["refresh_token"])
        except requests.RequestException as e:
            print(f"eBay token refresh failed for account {account_name}: {e}")
            continue

        account_state = state[account_name]

        try:
            orders = await asyncio.to_thread(fetch_ebay_orders, access_token)
        except EbayRateLimitError as e:
            print(f"eBay order check rate-limited for account {account_name}: {e}")
            await notify_ebay_rate_limit(channel)
            # Shared per-app quota (see ebay_rate_limited comment) - no point
            # trying the remaining accounts this cycle, they'd hit it too.
            return
        except requests.RequestException as e:
            print(f"eBay order check failed for account {account_name}: {e}")
            orders = None

        silent = account_state.get("silent_first_poll", False)
        for order in orders or []:
            order_id = order.get("orderId")
            if not order_id or order_id in account_state["seen_order_ids"]:
                continue
            if silent:
                account_state["seen_order_ids"].append(order_id)
                continue

            try:
                line_items = order.get("lineItems") or []
                title = line_items[0].get("title") if line_items else order_id
                buyer = (order.get("buyer") or {}).get("username", "unknown buyer")
                total = (order.get("pricingSummary") or {}).get("total") or {}
                price = f"{total.get('value', '?')} {total.get('currency', '')}".strip()

                await channel.send(f"🛒 New order: {title} — {buyer}, {price}.")
            except (discord.HTTPException, AttributeError, IndexError, TypeError) as e:
                print(f"Failed to notify about order {order_id} for account {account_name}: {e}")
                continue

            account_state["seen_order_ids"].append(order_id)
            save_ebay_state(state)

        try:
            conversations = await asyncio.to_thread(fetch_ebay_conversations, access_token)
        except EbayRateLimitError as e:
            print(f"eBay conversation check rate-limited for account {account_name}: {e}")
            await notify_ebay_rate_limit(channel)
            return
        except requests.RequestException as e:
            print(f"eBay conversation check failed for account {account_name}: {e}")
            conversations = None

        for convo in conversations or []:
            convo_id = convo.get("conversationId")
            if not convo_id:
                continue
            if not convo.get("unreadCount"):
                continue

            latest_message = convo.get("latestMessage") or {}
            ebay_message_id = latest_message.get("messageId")
            last_notified_id = account_state["seen_conversation_message_ids"].get(convo_id)
            if ebay_message_id and ebay_message_id == last_notified_id:
                continue
            if not ebay_message_id and convo_id in account_state["seen_conversation_message_ids"]:
                # No message id to compare against - fall back to the old skip-once behavior.
                continue
            if silent:
                account_state["seen_conversation_message_ids"][convo_id] = ebay_message_id
                continue

            try:
                if convo.get("conversationType") == "FROM_EBAY":
                    title = convo.get("conversationTitle", "")
                    await channel.send(f"📢 eBay notification: {title}")
                else:
                    buyer = latest_message.get("senderUsername", "unknown buyer")
                    preview = latest_message.get("messageBody", "")[:100]

                    await channel.send(f'💬 New eBay message from {buyer}: "{preview}"')
            except (discord.HTTPException, AttributeError, TypeError) as e:
                print(f"Failed to notify about conversation {convo_id} for account {account_name}: {e}")
                continue

            account_state["seen_conversation_message_ids"][convo_id] = ebay_message_id
            save_ebay_state(state)

        if silent and orders is not None and conversations is not None:
            del account_state["silent_first_poll"]
        save_ebay_state(state)

        # Skipped when the fetch failed: an empty list here would let the
        # first-run setup record nothing, then treat every old unanswered
        # conversation as new on the next poll.
        if conversations is not None and account["seller_username"]:
            await check_unreplied_conversations(
                channel, conversations, account_state, state, account["seller_username"], access_token
            )

    ebay_rate_limited = False


@tasks.loop(minutes=EBAY_TRADING_POLL_MINUTES)
async def check_ebay_trading_activity():
    async with ebay_state_lock:
        await _check_ebay_trading_activity()


async def _check_ebay_trading_activity():
    # Classic Trading API fallback, ricky.garage-specific. Deliberately its
    # own background loop, isolated from check_ebay_activity() above - a
    # different credential (shared with Lister Tool), a different protocol,
    # and a higher blast radius if anything ever went wrong here. See
    # ebay_trading.py's module docstring and claude.md before touching this.
    if not EBAY_TRADING_ENABLED:
        return

    account = next((a for a in EBAY_ACCOUNTS if a["name"] == EBAY_TRADING_ACCOUNT_NAME), None)
    if account is None:
        print(
            f"eBay Trading API fallback: no active EBAY_ACCOUNT entry named "
            f"{EBAY_TRADING_ACCOUNT_NAME!r} (EBAY_TRADING_ACCOUNT_NAME) - skipping this cycle."
        )
        return

    channel = client.get_channel(account["alert_channel_id"])
    if channel is None:
        print(f"eBay Trading API alert channel {account['alert_channel_id']} not found.")
        return

    state = load_ebay_state()
    trading_state = state[EBAY_TRADING_STATE_KEY]
    now = datetime.now(timezone.utc)
    # Two poll intervals + a 5-minute buffer: covers a slow-to-settle order,
    # and also one entirely failed poll (e.g. eBay error 518) without any
    # order falling between windows and being missed for good. The
    # overlap's duplicates are filtered out by the seen_order_ids diff below.
    window_start = now - timedelta(minutes=2 * EBAY_TRADING_POLL_MINUTES + 5)

    try:
        orders = await asyncio.to_thread(
            ebay_trading.fetch_trading_orders,
            EBAY_TRADING_CLIENT_ID, EBAY_TRADING_CLIENT_SECRET, EBAY_TRADING_REFRESH_TOKEN,
            window_start, now,
        )
    except (ebay_trading.TradingApiError, requests.RequestException) as e:
        print(f"eBay Trading API GetOrders failed: {e}")
        orders = []

    for order in orders:
        order_id = order.get("order_id")
        if not order_id or order_id in trading_state["seen_order_ids"]:
            continue

        try:
            title = order.get("title") or order_id
            buyer = order.get("buyer")
            price = f"{order.get('total') or '?'} {order.get('currency') or ''}".strip()
            await channel.send(f"🛒 New order (ricky.garage): {title} — {buyer}, {price}.")
        except discord.HTTPException as e:
            print(f"Failed to notify about Trading API order {order_id}: {e}")
            continue

        trading_state["seen_order_ids"].append(order_id)
        save_ebay_state(state)

    try:
        messages = await asyncio.to_thread(
            ebay_trading.fetch_trading_messages,
            EBAY_TRADING_CLIENT_ID, EBAY_TRADING_CLIENT_SECRET, EBAY_TRADING_REFRESH_TOKEN,
        )
    except (ebay_trading.TradingApiError, requests.RequestException) as e:
        print(f"eBay Trading API GetMemberMessages/GetMyMessages failed: {e}")
        messages = None

    if messages is None:
        return

    for msg in messages:
        message_id = msg.get("message_id")
        if not message_id or message_id in trading_state["seen_message_ids"]:
            continue

        if msg.get("replied"):
            # Genuinely already answered per GetMyMessages' per-message
            # Replied flag - NOT eBay's thread-batched MessageStatus, which
            # is confirmed unreliable (see ebay_trading.py, claude.md).
            # Nothing to alert on; record it so it's never re-evaluated.
            # Newer than anything still waiting from this buyer, so a reply
            # to it answers those too (see check_unreplied_trading_messages).
            sender = msg.get("sender")
            for pid in [pid for pid, p in trading_state["pending_replies"].items() if p["sender"] == sender]:
                del trading_state["pending_replies"][pid]
            trading_state["seen_message_ids"].append(message_id)
            save_ebay_state(state)
            continue

        try:
            sender = msg.get("sender")
            preview = truncate_for_discord(msg.get("body") or "", limit=300)
            await channel.send(f'💬 New unanswered message (ricky.garage) from {sender}: "{preview}"')
        except discord.HTTPException as e:
            print(f"Failed to notify about Trading API message {message_id}: {e}")
            continue

        trading_state["seen_message_ids"].append(message_id)
        trading_state["pending_replies"][message_id] = {
            "sender": sender,
            "body": preview,
            "since_utc": now.isoformat(),
        }
        save_ebay_state(state)

    await check_unreplied_trading_messages(channel, messages, trading_state, state, EBAY_TRADING_ACCOUNT_NAME)


@check_ebay_trading_activity.error
async def check_ebay_trading_activity_error(error):
    # discord.ext.tasks stops the loop on any unhandled exception rather than
    # silently retrying - correct behavior here, especially for a
    # TradingApiGuardrailError, which should halt everything until a human
    # looks at it, not keep looping as if nothing happened.
    print(f"eBay Trading API background loop stopped due to an error: {error!r}")
    if isinstance(error, ebay_trading.TradingApiGuardrailError):
        print("GUARDRAIL TRIP in ebay_trading.py - this must never happen. Investigate before restarting.")


@client.event
async def on_ready():
    await tree.sync()
    if not check_reminders.is_running():
        check_reminders.start()
    if EBAY_ACCOUNTS and not check_ebay_activity.is_running():
        check_ebay_activity.start()
    elif not EBAY_ACCOUNTS:
        print("eBay notifier not started: no active EBAY_ACCOUNT_*_NAME configured.")
    for account in EBAY_ACCOUNTS:
        if is_trading_fallback_account(account):
            print(f"eBay account {account['name']}: REST polling skipped, covered by the Trading API fallback instead.")
        elif not account["seller_username"]:
            print(
                f"EBAY_ACCOUNT_..._SELLER_USERNAME not set for account {account['name']}: 24h unreplied "
                "reminders are off, since it can't tell the seller's own replies apart from buyer messages."
            )
        channel = client.get_channel(account["alert_channel_id"])
        if channel is not None and not channel.permissions_for(channel.guild.me).mention_everyone:
            print(f"Missing 'Mention @everyone' permission in #{channel.name}: 24h reminders there won't ping anyone.")
    if EBAY_TRADING_ENABLED and not check_ebay_trading_activity.is_running():
        check_ebay_trading_activity.start()
    elif not EBAY_TRADING_ENABLED:
        print("eBay Trading API fallback not started: EBAY_TRADING_CLIENT_ID/CLIENT_SECRET/REFRESH_TOKEN/ACCOUNT_NAME not fully set.")
    print(f"Logged in as {client.user} — reminder bot is ready.")


client.run(BOT_TOKEN)
