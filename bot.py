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


def build_customer_message_embed(buyer, message_body):
    lines = []
    if EBAY_SELLER_USERNAME:
        lines.append(f"**{EBAY_SELLER_USERNAME}**")

    lines.append(f"💬 New customer message from {buyer}:")
    lines.append(f'"{message_body}"')

    return discord.Embed(description="\n".join(lines), color=discord.Color.blurple())


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


async def send_new_customer_notification(customer_channel, state, convo_id, buyer, message_body, message_link, ebay_message_id, now):
    embed = build_customer_message_embed(buyer, message_body)
    view = build_customer_message_view(convo_id, message_link)
    try:
        sent_message = await customer_channel.send(embed=embed, view=view)
    except discord.HTTPException as e:
        print(f"Failed to notify about customer conversation {convo_id}: {e}")
        return False

    state["customer_conversations"][convo_id] = {
        "first_seen_utc": now.isoformat(),
        "reminded": False,
        "buyer_username": buyer,
        "message_id": sent_message.id,
        "last_ebay_message_id": ebay_message_id,
    }
    # Persists independently of customer_conversations so that clicking "Mark
    # Conversation as Finished" (which clears the entry above) can't cause the
    # same still-unreplied eBay message to be re-announced on the next poll.
    state["customer_last_notified_message_ids"][convo_id] = ebay_message_id
    save_ebay_state(state)
    return True


async def process_customer_conversations(customer_channel, conversations, state):
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

            if message_id and message_id != entry.get("last_ebay_message_id"):
                # Same buyer, but a new message arrived on this still-open conversation -
                # notify again and restart the 24h reminder clock from this message.
                await send_new_customer_notification(
                    customer_channel, state, convo_id, sender, message_body, message_link, message_id, now
                )
                continue

            if entry.get("reminded"):
                continue

            first_seen = datetime.fromisoformat(entry["first_seen_utc"])
            if now - first_seen >= EBAY_CUSTOMER_REMINDER_DELAY:
                reminder_text = build_customer_reminder_text(sender, message_body)

                destination = customer_channel
                notification_message_id = entry.get("message_id")
                if notification_message_id:
                    try:
                        original_message = await customer_channel.fetch_message(notification_message_id)
                        thread = original_message.thread
                        if thread is None:
                            thread_name = truncate_thread_name(f"Unreplied: {sender}")
                            thread = await original_message.create_thread(name=thread_name)
                        destination = thread
                    except discord.HTTPException as e:
                        print(
                            f"Could not open/create reminder thread for customer conversation "
                            f"{convo_id}, falling back to channel: {e}"
                        )
                        destination = customer_channel

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
            await send_new_customer_notification(
                customer_channel, state, convo_id, buyer, message_body, message_link, message_id, now
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
        await process_customer_conversations(customer_channel, conversations, state)

    ebay_rate_limited = False


@client.event
async def on_ready():
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
