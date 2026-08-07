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

load_dotenv()

BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
TIMEZONE_NAME = os.environ.get("BOT_TIMEZONE", "Asia/Jakarta")
LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)

DATA_FILE = "reminders.json"

EBAY_REFRESH_TOKEN = os.environ.get("EBAY_REFRESH_TOKEN")
EBAY_ALERT_CHANNEL_ID = (
    int(os.environ["EBAY_ALERT_CHANNEL_ID"]) if os.environ.get("EBAY_ALERT_CHANNEL_ID") else None
)
EBAY_CUSTOMER_CHANNEL_ID = (
    int(os.environ["EBAY_CUSTOMER_CHANNEL_ID"]) if os.environ.get("EBAY_CUSTOMER_CHANNEL_ID") else None
)
EBAY_SELLER_USERNAME = os.environ.get("EBAY_SELLER_USERNAME")
EBAY_ORDER_URL = "https://api.ebay.com/sell/fulfillment/v1/order"
EBAY_CONVERSATION_URL = "https://api.ebay.com/commerce/message/v1/conversation"
EBAY_STATE_FILE = "ebay_state.json"
EBAY_RATE_LIMIT_MESSAGE = "⚠️ eBay API rate limit reached — skipping this check, will retry next cycle"
EBAY_CUSTOMER_REMINDER_DELAY = timedelta(hours=24)

ebay_rate_limited = False
# Populated once in on_ready() via application_info() - doesn't need the
# privileged GUILD_MEMBERS intent, unlike guild.members/guild.fetch_members().
bot_owner_id = None


class EbayRateLimitError(Exception):
    pass

FINISH_CONVO_CUSTOM_ID_TEMPLATE = r"finish_convo:(?P<convo_id>[A-Za-z0-9_-]+)"


class ReminderBotClient(discord.Client):
    async def setup_hook(self):
        self.add_dynamic_items(FinishConversationButton)


intents = discord.Intents.default()
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
    if os.path.exists(EBAY_STATE_FILE):
        with open(EBAY_STATE_FILE, "r") as f:
            state = json.load(f)
    else:
        state = {}

    state.setdefault("seen_order_ids", [])
    state.setdefault("seen_conversation_ids", [])
    state.setdefault("seen_conversation_message_ids", {})
    state.setdefault("customer_conversations", {})
    state.setdefault("customer_last_notified_message_ids", {})
    # Survives customer_conversations entries being cleared (seller-reply
    # resolution, "Mark as Finished") so a conversation that goes quiet and
    # then gets a new buyer message can reopen its existing thread instead of
    # creating a duplicate one and re-backfilling the whole history.
    state.setdefault("customer_conversation_thread_ids", {})
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
        state = load_ebay_state()
        state["customer_conversations"].pop(self.convo_id, None)
        save_ebay_state(state)

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

        await interaction.response.edit_message(embed=embed, view=disabled_view)
        await interaction.followup.send("Marked this conversation as finished.", ephemeral=True)


def build_root_conversation_embed(buyer):
    lines = []
    if EBAY_SELLER_USERNAME:
        lines.append(f"eBay ID: {EBAY_SELLER_USERNAME}")
    lines.append(f"💬 Conversation with user {buyer}:")

    return discord.Embed(description="\n".join(lines), color=discord.Color.blurple())


def format_thread_message(sender, message_body):
    return f"**{sender}:** {message_body}"


def build_customer_reminder_text(buyer, message_body):
    lines = []
    if EBAY_SELLER_USERNAME:
        lines.append(f"**{EBAY_SELLER_USERNAME}**")

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


async def add_owner_to_thread(thread, convo_id):
    # @everyone inside a thread only reaches members already in that thread's
    # membership list - it doesn't add anyone (confirmed empirically, see
    # claude.md). Explicitly adding the owner here is what makes @everyone
    # actually work for later thread activity (24h escalations, etc).
    # guild.members/guild.fetch_members() need the privileged GUILD_MEMBERS
    # intent (not enabled for this app); add_user() with a known id doesn't.
    if bot_owner_id is None:
        return
    try:
        await thread.add_user(discord.Object(id=bot_owner_id))
    except discord.HTTPException as e:
        print(f"Could not add owner to thread for customer conversation {convo_id}: {e}")


async def get_or_create_conversation_thread(customer_channel, state, convo_id, entry):
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
            await add_owner_to_thread(thread, convo_id)
    except discord.HTTPException as e:
        print(f"Could not open/create thread for customer conversation {convo_id}: {e}")
        return None

    entry["thread_id"] = thread.id
    state["customer_conversation_thread_ids"][convo_id] = thread.id
    save_ebay_state(state)
    return thread


async def create_new_conversation_thread(
    customer_channel, state, access_token, convo_id, buyer, message_link,
    fallback_message_id, fallback_message_body, now,
):
    embed = build_root_conversation_embed(buyer)
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
        await add_owner_to_thread(thread, convo_id)
    except discord.HTTPException as e:
        print(f"Failed to create thread for customer conversation {convo_id}: {e}")
        thread = None

    try:
        history = fetch_ebay_conversation_history(access_token, convo_id)
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
    save_ebay_state(state)
    return True


async def post_new_thread_message(customer_channel, state, convo_id, entry, sender, message_body, message_id, now):
    thread = await get_or_create_conversation_thread(customer_channel, state, convo_id, entry)
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
    save_ebay_state(state)
    return True


async def reopen_conversation_thread(customer_channel, state, convo_id, thread_id, buyer, message_body, message_id, now):
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
    save_ebay_state(state)
    return True


async def process_customer_conversations(customer_channel, conversations, state, access_token):
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
                save_ebay_state(state)
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
                    customer_channel, state, convo_id, entry, sender, message_body, message_id, now
                )
                continue

            if entry.get("reminded"):
                continue

            first_seen = datetime.fromisoformat(entry["first_seen_utc"])
            if now - first_seen >= EBAY_CUSTOMER_REMINDER_DELAY:
                reminder_text = build_customer_reminder_text(sender, message_body)
                # NOTE: unlike the root notification, this sends inside the
                # conversation's THREAD - @everyone in a thread only notifies
                # members already in that thread's membership list, and (unlike
                # an individual <@user_id> mention) does not add anyone to it.
                # This is why add_owner_to_thread() explicitly adds the owner as
                # a real thread member at creation time (both in
                # create_new_conversation_thread and the legacy self-heal path
                # below) - without that, this @everyone would only reach whoever
                # already happened to be in the thread. See claude.md gotchas.
                reminder_text = f"@everyone {reminder_text}"

                thread = await get_or_create_conversation_thread(customer_channel, state, convo_id, entry)
                destination = thread if thread is not None else customer_channel

                try:
                    await destination.send(reminder_text)
                except discord.HTTPException as e:
                    print(f"Failed to send reminder for customer conversation {convo_id}: {e}")
                    continue

                entry["reminded"] = True
                save_ebay_state(state)
        else:
            if sender == EBAY_SELLER_USERNAME:
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
                    customer_channel, state, convo_id, existing_thread_id, buyer, message_body, message_id, now
                )
            if reopened is False:
                # No known thread for this conversation, or it's genuinely
                # gone/broken - fall back to creating fresh. Deliberately does
                # NOT fall back when reopened is None (a transient error, e.g.
                # rate limit) - that should just retry next poll, not create a
                # duplicate thread over what might still be a perfectly good one.
                await create_new_conversation_thread(
                    customer_channel, state, access_token, convo_id, buyer, message_link,
                    message_id, message_body, now,
                )


@tasks.loop(minutes=2)
async def check_ebay_activity():
    global ebay_rate_limited

    channel = client.get_channel(EBAY_ALERT_CHANNEL_ID)
    if channel is None:
        print(f"eBay alert channel {EBAY_ALERT_CHANNEL_ID} not found.")
        return

    customer_channel = None
    if EBAY_CUSTOMER_CHANNEL_ID:
        customer_channel = client.get_channel(EBAY_CUSTOMER_CHANNEL_ID)
        if customer_channel is None:
            print(f"eBay customer channel {EBAY_CUSTOMER_CHANNEL_ID} not found.")

    try:
        access_token = get_access_token(EBAY_REFRESH_TOKEN)
    except requests.RequestException as e:
        print(f"eBay token refresh failed: {e}")
        return

    state = load_ebay_state()

    try:
        orders = fetch_ebay_orders(access_token)
    except EbayRateLimitError as e:
        print(f"eBay order check rate-limited: {e}")
        await notify_ebay_rate_limit(channel)
        return
    except requests.RequestException as e:
        print(f"eBay order check failed: {e}")
        orders = []

    for order in orders:
        order_id = order.get("orderId")
        if not order_id or order_id in state["seen_order_ids"]:
            continue

        try:
            line_items = order.get("lineItems") or []
            title = line_items[0].get("title") if line_items else order_id
            buyer = (order.get("buyer") or {}).get("username", "unknown buyer")
            total = (order.get("pricingSummary") or {}).get("total") or {}
            price = f"{total.get('value', '?')} {total.get('currency', '')}".strip()

            await channel.send(f"🛒 New order: {title} — {buyer}, {price}.")
        except (discord.HTTPException, AttributeError, IndexError, TypeError) as e:
            print(f"Failed to notify about order {order_id}: {e}")
            continue

        state["seen_order_ids"].append(order_id)
        save_ebay_state(state)

    try:
        conversations = fetch_ebay_conversations(access_token)
    except EbayRateLimitError as e:
        print(f"eBay conversation check rate-limited: {e}")
        await notify_ebay_rate_limit(channel)
        return
    except requests.RequestException as e:
        print(f"eBay conversation check failed: {e}")
        conversations = []

    for convo in conversations:
        convo_id = convo.get("conversationId")
        if not convo_id:
            continue
        if not convo.get("unreadCount"):
            continue

        latest_message = convo.get("latestMessage") or {}
        ebay_message_id = latest_message.get("messageId")
        last_notified_id = state["seen_conversation_message_ids"].get(convo_id)
        if ebay_message_id and ebay_message_id == last_notified_id:
            continue
        if not ebay_message_id and convo_id in state["seen_conversation_message_ids"]:
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
            print(f"Failed to notify about conversation {convo_id}: {e}")
            continue

        state["seen_conversation_message_ids"][convo_id] = ebay_message_id
        save_ebay_state(state)

    if customer_channel is not None:
        await process_customer_conversations(customer_channel, conversations, state, access_token)

    ebay_rate_limited = False


@client.event
async def on_ready():
    global bot_owner_id
    if bot_owner_id is None:
        try:
            app_info = await client.application_info()
            bot_owner_id = app_info.owner.id
        except discord.HTTPException as e:
            print(f"Could not fetch application owner for thread auto-add: {e}")

    await tree.sync()
    if not check_reminders.is_running():
        check_reminders.start()
    if EBAY_REFRESH_TOKEN and EBAY_ALERT_CHANNEL_ID and not check_ebay_activity.is_running():
        check_ebay_activity.start()
    elif not (EBAY_REFRESH_TOKEN and EBAY_ALERT_CHANNEL_ID):
        print("eBay notifier not started: EBAY_REFRESH_TOKEN or EBAY_ALERT_CHANNEL_ID not set.")
    if not EBAY_CUSTOMER_CHANNEL_ID:
        print("eBay customer-message channel not configured (EBAY_CUSTOMER_CHANNEL_ID not set).")
    elif not EBAY_SELLER_USERNAME:
        print(
            "EBAY_SELLER_USERNAME not set: customer-message tracking can't tell the seller's own "
            "replies apart from new customer messages."
        )
    print(f"Logged in as {client.user} — reminder bot is ready.")


client.run(BOT_TOKEN)
