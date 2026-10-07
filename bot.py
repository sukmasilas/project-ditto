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
        customer_channel_id_raw = os.environ.get(f"{prefix}CUSTOMER_CHANNEL_ID")

        if refresh_token and alert_channel_id_raw:
            accounts.append({
                "name": name,
                "refresh_token": refresh_token,
                "seller_username": os.environ.get(f"{prefix}SELLER_USERNAME"),
                "alert_channel_id": int(alert_channel_id_raw),
                "customer_channel_id": int(customer_channel_id_raw) if customer_channel_id_raw else None,
            })
        else:
            print(f"eBay account '{name}' (EBAY_ACCOUNT_{i}_*) skipped: needs REFRESH_TOKEN and ALERT_CHANNEL_ID to be active.")

        i += 1
    return accounts


EBAY_ACCOUNTS = load_ebay_accounts()


def get_customer_account():
    # Customer-message threading is single-account only for now (see
    # claude.md) - if a second customer-channel account is ever added, this
    # (and FinishConversationButton's custom_id, which doesn't carry account
    # identity) will need to change to disambiguate which account a click
    # belongs to.
    return next((a for a in EBAY_ACCOUNTS if a["customer_channel_id"]), None)


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
# How often the Trading API fallback polls. Defaults to 10 minutes (was a
# hardcoded 2): every poll is 3+ Trading API calls on the app shared with
# Lister Tool, and eBay was returning error 518 ("exceeded usage limit on
# this call") at the 2-minute rate.
EBAY_TRADING_POLL_MINUTES = max(1, int(os.environ.get("EBAY_TRADING_POLL_MINUTES") or "10"))


EBAY_ORDER_URL = "https://api.ebay.com/sell/fulfillment/v1/order"
EBAY_CONVERSATION_URL = "https://api.ebay.com/commerce/message/v1/conversation"
EBAY_STATE_FILE = "ebay_state.json"
EBAY_RATE_LIMIT_MESSAGE = "⚠️ eBay API rate limit reached — skipping this check, will retry next cycle"
EBAY_CUSTOMER_REMINDER_DELAY = timedelta(hours=24)
# (connect, read) seconds - requests has no default timeout.
EBAY_HTTP_TIMEOUT = (10, 30)

# Every eBay HTTP call runs via asyncio.to_thread so a slow eBay response
# can't block the event loop (it used to, which starved discord.py's gateway
# heartbeat and made Discord drop the connection - hundreds of "heartbeat
# blocked" warnings a week). The catch: with the loop no longer frozen, the
# two eBay polling loops and the Finish button can now genuinely interleave,
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

FINISH_CONVO_CUSTOM_ID_TEMPLATE = r"finish_convo:(?P<convo_id>[A-Za-z0-9_-]+)"


class ReminderBotClient(discord.Client):
    async def setup_hook(self):
        self.add_dynamic_items(FinishConversationButton)


intents = discord.Intents.default()
# Privileged intent, now enabled in the Developer Portal - needed so
# guild.members actually returns real members instead of just the bot
# (see add_all_members_to_thread and claude.md gotchas).
intents.members = True
client = ReminderBotClient(intents=intents)
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
        account_state = state.setdefault(account["name"], {})
        account_state.setdefault("seen_order_ids", [])
        account_state.setdefault("seen_conversation_message_ids", {})
        account_state.setdefault("customer_conversations", {})
        account_state.setdefault("customer_last_notified_message_ids", {})
        # Survives customer_conversations entries being cleared (seller-reply
        # resolution, "Mark as Finished") so a conversation that goes quiet
        # and then gets a new buyer message can reopen its existing thread
        # instead of creating a duplicate one and re-backfilling the history.
        account_state.setdefault("customer_conversation_thread_ids", {})

    if EBAY_TRADING_ENABLED:
        # Deliberately its own namespace, not merged into the REST-based
        # account_state above - different API, different ID space (eBay
        # Trading API OrderID/MessageID vs the REST orderId/messageId), kept
        # separate so the two dedup mechanisms can't be confused.
        trading_state = state.setdefault(EBAY_TRADING_STATE_KEY, {})
        trading_state.setdefault("seen_order_ids", [])
        trading_state.setdefault("seen_message_ids", [])

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


def fetch_ebay_conversation_history(access_token, convo_id):
    """Full message history for one conversation, oldest-first.

    eBay's singular getConversation endpoint returns messages newest-first
    and paginates via limit/offset/total - confirmed empirically against the
    live API (docs site was unreachable), not documented anywhere obvious.
    """
    messages = []
    offset = 0
    limit = 25
    while True:
        response = requests.get(
            f"{EBAY_CONVERSATION_URL}/{convo_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=EBAY_HTTP_TIMEOUT,
            params={"conversation_type": "FROM_MEMBERS", "offset": offset, "limit": limit},
        )
        if is_ebay_rate_limit_response(response):
            raise EbayRateLimitError("eBay conversation history API rate limit reached")
        response.raise_for_status()
        data = response.json()
        batch = data.get("messages", [])
        messages.extend(batch)
        total = data.get("total", len(messages))
        offset += len(batch)
        if not batch or offset >= total:
            break

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


def truncate_for_discord(text, limit=1800):
    if len(text) > limit:
        return text[:limit].rstrip() + "… (truncated)"
    return text


def truncate_thread_name(name, limit=100):
    if len(name) > limit:
        return name[: limit - 1].rstrip() + "…"
    return name


class FinishConversationButton(
    discord.ui.DynamicItem[discord.ui.Button], template=FINISH_CONVO_CUSTOM_ID_TEMPLATE
):
    def __init__(self, convo_id):
        super().__init__(
            discord.ui.Button(
                label="Mark Conversation as Finished",
                style=discord.ButtonStyle.secondary,
                custom_id=f"finish_convo:{convo_id}",
            )
        )
        self.convo_id = convo_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["convo_id"])

    async def callback(self, interaction: discord.Interaction):
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.description = f"{embed.description or ''}\n\n✅ Marked as finished by {interaction.user.mention}"

        disabled_view = discord.ui.View(timeout=None)
        for row in interaction.message.components:
            for component in row.children:
                if isinstance(component, discord.Button) and component.style == discord.ButtonStyle.link:
                    disabled_view.add_item(
                        discord.ui.Button(label=component.label, style=discord.ButtonStyle.link, url=component.url)
                    )
        disabled_view.add_item(
            discord.ui.Button(
                label="Mark Conversation as Finished",
                style=discord.ButtonStyle.secondary,
                custom_id=self.item.custom_id,
                disabled=True,
            )
        )

        # Respond before touching state: Discord requires an interaction
        # response within 3s, and a poll may be holding ebay_state_lock for
        # longer than that while it waits on eBay.
        await interaction.response.edit_message(embed=embed, view=disabled_view)

        async with ebay_state_lock:
            state = load_ebay_state()
            account = get_customer_account()
            if account is not None:
                state[account["name"]]["customer_conversations"].pop(self.convo_id, None)
                save_ebay_state(state)

        await interaction.followup.send("Marked this conversation as finished.", ephemeral=True)


def build_root_conversation_embed(buyer, seller_username):
    lines = []
    if seller_username:
        lines.append(f"eBay ID: {seller_username}")
    lines.append(f"💬 Conversation with user {buyer}:")

    return discord.Embed(description="\n".join(lines), color=discord.Color.blurple())


def format_thread_message(sender, message_body):
    return f"**{sender}:** {message_body}"


def build_customer_reminder_text(buyer, message_body, seller_username):
    lines = []
    if seller_username:
        lines.append(f"**{seller_username}**")

    lines.append(f"⏰ Still unreplied after 24h — customer message from {buyer}:")
    lines.append(f'"{message_body}"')

    return "\n".join(lines)


def build_customer_message_view(convo_id, message_link):
    view = discord.ui.View(timeout=None)
    if message_link:
        view.add_item(discord.ui.Button(label="Reply on eBay", style=discord.ButtonStyle.link, url=message_link))
    view.add_item(FinishConversationButton(convo_id))
    return view


async def get_conversation_thread(customer_channel, thread_id):
    if not thread_id:
        return None
    thread = client.get_channel(thread_id)
    if thread is not None:
        return thread
    try:
        return await customer_channel.guild.fetch_channel(thread_id)
    except discord.HTTPException:
        return None


async def add_all_members_to_thread(thread, convo_id):
    # @everyone inside a thread only reaches members already in that thread's
    # membership list - it doesn't add anyone (confirmed empirically, see
    # claude.md). Explicitly adding everyone here is what makes @everyone
    # actually work for later thread activity (24h escalations, etc).
    # Requires the privileged GUILD_MEMBERS intent (now enabled) - guild.members
    # comes back fully populated by the time on_ready() fires, no explicit
    # guild.chunk()/fetch_members() call needed, confirmed empirically.
    #
    # Known limitation, not a bug: only members present in the server at
    # thread-creation time get added. Someone who joins later is NOT
    # retroactively added to already-existing threads.
    for member in thread.guild.members:
        if member.bot:
            continue
        try:
            await thread.add_user(member)
        except discord.HTTPException as e:
            print(f"Could not add {member} to thread for customer conversation {convo_id}: {e}")


async def get_or_create_conversation_thread(customer_channel, state, full_state, convo_id, entry):
    thread = await get_conversation_thread(customer_channel, entry.get("thread_id"))
    if thread is not None:
        return thread

    # Legacy conversations tracked before the per-conversation-thread redesign
    # have no thread_id - derive/create one from the root notification message
    # and persist it so future lookups take the fast path above.
    notification_message_id = entry.get("message_id")
    if not notification_message_id:
        return None
    try:
        original_message = await customer_channel.fetch_message(notification_message_id)
        thread = original_message.thread
        if thread is None:
            thread_name = truncate_thread_name(f"eBay: {entry.get('buyer_username', 'buyer')}")
            thread = await original_message.create_thread(name=thread_name)
            await add_all_members_to_thread(thread, convo_id)
    except discord.HTTPException as e:
        print(f"Could not open/create thread for customer conversation {convo_id}: {e}")
        return None

    entry["thread_id"] = thread.id
    state["customer_conversation_thread_ids"][convo_id] = thread.id
    save_ebay_state(full_state)
    return thread


async def create_new_conversation_thread(
    customer_channel, state, full_state, access_token, convo_id, buyer, message_link,
    fallback_message_id, fallback_message_body, now, seller_username,
):
    embed = build_root_conversation_embed(buyer, seller_username)
    view = build_customer_message_view(convo_id, message_link)
    # Mentions only trigger a real Discord notification when they're in the
    # message content (or a component) - a mention inside an embed never pings.
    # No allowed_mentions is set anywhere (client or per-call), so this payload
    # omits that field entirely and Discord's own default applies, which lets
    # @everyone through (subject to the bot's "Mention @everyone" permission) -
    # confirmed against discord.py's http.handle_message_parameters source.
    try:
        root_message = await customer_channel.send(content="@everyone", embed=embed, view=view)
    except discord.HTTPException as e:
        print(f"Failed to post root notification for customer conversation {convo_id}: {e}")
        return False

    thread_name = truncate_thread_name(f"eBay: {buyer}")
    try:
        thread = await root_message.create_thread(name=thread_name)
        await add_all_members_to_thread(thread, convo_id)
    except discord.HTTPException as e:
        print(f"Failed to create thread for customer conversation {convo_id}: {e}")
        thread = None

    try:
        history = await asyncio.to_thread(fetch_ebay_conversation_history, access_token, convo_id)
    except (EbayRateLimitError, requests.RequestException) as e:
        print(f"Failed to fetch message history for customer conversation {convo_id}: {e}")
        history = []

    if not history and fallback_message_id:
        # getConversation failed - fall back to just the single message that
        # triggered this notification so the thread isn't left empty.
        history = [{
            "messageId": fallback_message_id,
            "messageBody": fallback_message_body,
            "senderUsername": buyer,
        }]

    posted_ids = []
    if thread is not None:
        for msg in history:
            body = truncate_for_discord(msg.get("messageBody", ""))
            sender = msg.get("senderUsername", "unknown")
            try:
                await thread.send(format_thread_message(sender, body))
            except discord.HTTPException as e:
                print(f"Failed to post history message into thread for customer conversation {convo_id}: {e}")
                continue
            msg_id = msg.get("messageId")
            if msg_id:
                posted_ids.append(msg_id)

    last_message_id = history[-1]["messageId"] if history else fallback_message_id

    state["customer_conversations"][convo_id] = {
        "first_seen_utc": now.isoformat(),
        "reminded": False,
        "buyer_username": buyer,
        "message_id": root_message.id,
        "thread_id": thread.id if thread is not None else None,
        "last_ebay_message_id": last_message_id,
        "posted_message_ids": posted_ids,
    }
    if thread is not None:
        state["customer_conversation_thread_ids"][convo_id] = thread.id
    # Persists independently of customer_conversations so that clicking "Mark
    # Conversation as Finished" (which clears the entry above) can't cause the
    # same still-unreplied eBay message to be re-announced on the next poll.
    state["customer_last_notified_message_ids"][convo_id] = last_message_id
    save_ebay_state(full_state)
    return True


async def post_new_thread_message(customer_channel, state, full_state, convo_id, entry, sender, message_body, message_id, now):
    thread = await get_or_create_conversation_thread(customer_channel, state, full_state, convo_id, entry)
    destination = thread if thread is not None else customer_channel

    try:
        await destination.send(format_thread_message(sender, message_body))
    except discord.HTTPException as e:
        print(f"Failed to post new message for customer conversation {convo_id}: {e}")
        return False

    # New message restarts the 24h reminder clock.
    entry["first_seen_utc"] = now.isoformat()
    entry["reminded"] = False
    entry["last_ebay_message_id"] = message_id
    # Deliberately indexes rather than setdefault()s: the caller is expected to
    # have already seeded this for legacy entries, so a missing key here should
    # fail loudly instead of silently masking a re-introduced backfill gap.
    posted_ids = entry["posted_message_ids"]
    if message_id not in posted_ids:
        posted_ids.append(message_id)
    state["customer_last_notified_message_ids"][convo_id] = message_id
    save_ebay_state(full_state)
    return True


async def reopen_conversation_thread(customer_channel, state, full_state, convo_id, thread_id, buyer, message_body, message_id, now):
    # Conversation went quiet (seller replied, or "Mark as Finished") and its
    # customer_conversations entry was cleared, but the buyer just wrote again.
    # Reuse the existing thread instead of creating a new one and re-backfilling
    # the whole history from scratch - the whole point of customer_conversation_thread_ids.
    #
    # Returns True (reopened), False (thread genuinely gone/broken - safe for
    # the caller to create a fresh one), or None (transient failure - caller
    # should skip this poll and retry later, NOT fall back to creating a
    # duplicate thread just because of a momentary rate limit or 5xx).
    thread = client.get_channel(thread_id)
    if thread is None:
        try:
            thread = await customer_channel.guild.fetch_channel(thread_id)
        except discord.NotFound:
            return False
        except discord.HTTPException as e:
            print(f"Transient error resolving thread for customer conversation {convo_id}, will retry next poll: {e}")
            return None

    try:
        await thread.send(format_thread_message(buyer, message_body))
    except discord.HTTPException as e:
        print(f"Failed to post reopened message for customer conversation {convo_id}: {e}")
        return False

    state["customer_conversations"][convo_id] = {
        "first_seen_utc": now.isoformat(),
        "reminded": False,
        "buyer_username": buyer,
        "message_id": thread.id,  # thread shares its id with the message that started it
        "thread_id": thread.id,
        "last_ebay_message_id": message_id,
        "posted_message_ids": [message_id] if message_id else [],
    }
    state["customer_last_notified_message_ids"][convo_id] = message_id
    save_ebay_state(full_state)
    return True


async def process_customer_conversations(customer_channel, conversations, state, full_state, seller_username, access_token):
    now = datetime.now(timezone.utc)

    for convo in conversations:
        if convo.get("conversationType") != "FROM_MEMBERS":
            continue

        convo_id = convo.get("conversationId")
        if not convo_id:
            continue

        latest_message = convo.get("latestMessage") or {}
        sender = latest_message.get("senderUsername", "unknown buyer")
        message_body = truncate_for_discord(latest_message.get("messageBody", ""))
        message_id = latest_message.get("messageId")
        message_link = (
            f"https://www.ebay.com/cnt/viewMessage?group_type=CORE&question_id={message_id}"
            if message_id
            else None
        )

        entry = state["customer_conversations"].get(convo_id)

        if entry is not None:
            if sender != entry.get("buyer_username"):
                # The seller has since sent a message back - resolved.
                del state["customer_conversations"][convo_id]
                save_ebay_state(full_state)
                continue

            if "posted_message_ids" not in entry:
                # Legacy entry from before this field existed - seed it from
                # last_ebay_message_id so its already-announced message isn't
                # mistaken for new and reposted (see claude.md backfill gotcha).
                seed_id = entry.get("last_ebay_message_id")
                entry["posted_message_ids"] = [seed_id] if seed_id else []
            posted_ids = entry["posted_message_ids"]
            if message_id and message_id not in posted_ids:
                # Same buyer, but a new message arrived on this still-open conversation -
                # post it into the existing thread and restart the 24h reminder clock.
                await post_new_thread_message(
                    customer_channel, state, full_state, convo_id, entry, sender, message_body, message_id, now
                )
                continue

            if entry.get("reminded"):
                continue

            first_seen = datetime.fromisoformat(entry["first_seen_utc"])
            if now - first_seen >= EBAY_CUSTOMER_REMINDER_DELAY:
                reminder_text = build_customer_reminder_text(sender, message_body, seller_username)
                # NOTE: unlike the root notification, this sends inside the
                # conversation's THREAD - @everyone in a thread only notifies
                # members already in that thread's membership list, and (unlike
                # an individual <@user_id> mention) does not add anyone to it.
                # This is why add_all_members_to_thread() explicitly adds every
                # real member at creation time (both in
                # create_new_conversation_thread and the legacy self-heal path
                # below) - without that, this @everyone would only reach whoever
                # already happened to be in the thread. See claude.md gotchas.
                reminder_text = f"@everyone {reminder_text}"

                thread = await get_or_create_conversation_thread(customer_channel, state, full_state, convo_id, entry)
                destination = thread if thread is not None else customer_channel

                try:
                    await destination.send(reminder_text)
                except discord.HTTPException as e:
                    print(f"Failed to send reminder for customer conversation {convo_id}: {e}")
                    continue

                entry["reminded"] = True
                save_ebay_state(full_state)
        else:
            if sender == seller_username:
                # Latest message is the seller's own reply - nothing new to track.
                continue

            if message_id and message_id == state["customer_last_notified_message_ids"].get(convo_id):
                # Already notified about this exact message before (e.g. it was manually
                # marked finished without an actual reply on eBay) - don't re-announce it.
                continue

            buyer = sender
            existing_thread_id = state["customer_conversation_thread_ids"].get(convo_id)
            reopened = False
            if existing_thread_id:
                reopened = await reopen_conversation_thread(
                    customer_channel, state, full_state, convo_id, existing_thread_id, buyer, message_body, message_id, now
                )
            if reopened is False:
                # No known thread for this conversation, or it's genuinely
                # gone/broken - fall back to creating fresh. Deliberately does
                # NOT fall back when reopened is None (a transient error, e.g.
                # rate limit) - that should just retry next poll, not create a
                # duplicate thread over what might still be a perfectly good one.
                await create_new_conversation_thread(
                    customer_channel, state, full_state, access_token, convo_id, buyer, message_link,
                    message_id, message_body, now, seller_username,
                )


@tasks.loop(minutes=2)
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

        channel = client.get_channel(account["alert_channel_id"])
        if channel is None:
            print(f"eBay alert channel {account['alert_channel_id']} not found for account {account_name}.")
            continue

        customer_channel = None
        if account["customer_channel_id"]:
            customer_channel = client.get_channel(account["customer_channel_id"])
            if customer_channel is None:
                print(f"eBay customer channel {account['customer_channel_id']} not found for account {account_name}.")

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
            orders = []

        for order in orders:
            order_id = order.get("orderId")
            if not order_id or order_id in account_state["seen_order_ids"]:
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
            conversations = []

        for convo in conversations:
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

        if customer_channel is not None:
            await process_customer_conversations(
                customer_channel, conversations, account_state, state, account["seller_username"], access_token
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
        messages = []

    for msg in messages:
        message_id = msg.get("message_id")
        if not message_id or message_id in trading_state["seen_message_ids"]:
            continue

        if msg.get("replied"):
            # Genuinely already answered per GetMyMessages' per-message
            # Replied flag - NOT eBay's thread-batched MessageStatus, which
            # is confirmed unreliable (see ebay_trading.py, claude.md).
            # Nothing to alert on; record it so it's never re-evaluated.
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
        save_ebay_state(state)


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
        if not account["customer_channel_id"]:
            print(f"eBay customer-message channel not configured for account {account['name']} (general activity feed only).")
        elif not account["seller_username"]:
            print(
                f"EBAY_ACCOUNT_..._SELLER_USERNAME not set for account {account['name']}: customer-message "
                "tracking can't tell the seller's own replies apart from new customer messages."
            )
    if EBAY_TRADING_ENABLED and not check_ebay_trading_activity.is_running():
        check_ebay_trading_activity.start()
    elif not EBAY_TRADING_ENABLED:
        print("eBay Trading API fallback not started: EBAY_TRADING_CLIENT_ID/CLIENT_SECRET/REFRESH_TOKEN/ACCOUNT_NAME not fully set.")
    print(f"Logged in as {client.user} — reminder bot is ready.")


client.run(BOT_TOKEN)
