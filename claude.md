# Project Ditto — Discord Command Center

## What this is

A Discord bot (Application: **Ditto00**, PM2 process name: **project-ditto**) that acts as a personal/business command center. It currently does two things:

1. **Personal reminders** — slash commands to set, list, and cancel reminders that post back to the channel when due.
2. **eBay integration** — polls the eBay API for account activity (orders, buyer messages, eBay's own system notices) and posts it to Discord, including a dedicated customer-message channel with reply-tracking, 24h "still unanswered" escalation, deep links back to eBay, and interactive buttons.

The long-term vision is a genuine "command center" — more services (starting with eBay) feeding into Discord as the single place the owner monitors and acts from.

## Architecture

- **Language/framework:** Python, `discord.py` (slash commands via `app_commands`, background polling via `discord.ext.tasks`)
- **State:** flat JSON files (`reminders.json`, `ebay_state.json`) — no database. State is written by the live process on the server; local copies are dev-time scratch only and can go stale.
- **Local dev folder:** `~/Projects/project-ditto` (Mac laptop)
- **Server:** DigitalOcean droplet, IP `157.230.47.237`, Ubuntu 24.04, SSH key at `~/.ssh/id_ed25519_do`
  - Deployed to `/root/project-ditto` on the server
  - Runs inside a **venv** (`/root/project-ditto/venv`) — Ubuntu 24.04 blocks system-wide `pip install` (PEP 668), don't fight this with `--break-system-packages`
  - Process managed by **PM2** (not systemd) as `project-ditto` — the server already ran PM2 for other apps before this project existed
  - **This server also runs unrelated apps** (`dodoco-pos`, `lister-tool` at minimum) — never touch, restart, or affect these when deploying or debugging this bot
- **eBay auth:** OAuth2, credentials in `ebay_auth.py` (shared token-refresh logic used by both `bot.py` and the one-time setup script `get_ebay_token.py`)

## File structure

| File | Purpose |
|---|---|
| `bot.py` | Main bot — commands, background polling loops, all feature logic |
| `ebay_auth.py` | Shared eBay OAuth token exchange/refresh logic |
| `get_ebay_token.py` | One-time interactive script to complete the OAuth consent flow and mint a refresh token |
| `requirements.txt` | Python dependencies |
| `.env` | Real secrets — **never commit, never paste into chat** |
| `.env.example` | Template showing which env vars are needed, no real values |
| `README.md` | Human setup guide (Discord + eBay + deployment steps) |
| `reminders.json`, `ebay_state.json` | Runtime state — treat as data, not code |

## Critical rules

**Secrets never go through chat.** `.env` values (Discord token, eBay App ID/Cert ID/refresh token) get typed directly into the file via `nano` on whichever machine needs them, never pasted into a Claude Code prompt or shown in a message. If a value ever does leak into a chat transcript by accident, don't panic-rotate for a personal project, but don't make it a habit.

**Never hardcode what should be configurable.** Timezone, channel IDs, seller username, reminder thresholds — all env vars, not literals in code. This project will likely support multiple eBay accounts eventually; flat single-value env vars are a known limitation to revisit then, not something to paper over with more hardcoding now.

**Deploys to the server must be targeted, never a directory sync.** Only `scp` the specific files that changed (`bot.py`, `.env`, etc.). A wildcard or whole-folder sync risks overwriting the server's live, up-to-date state files (`ebay_state.json`, `reminders.json`) with stale local copies — this has caused real bugs before (mass re-notification of already-handled messages).

**Verify third-party API facts against real docs before building against them.** This project has been burned more than once by guessing eBay API details (scope names, response field names) instead of checking — cost real debugging cycles each time. When unsure about an external API's exact shape, fetch the real docs or empirically inspect a raw response before writing code against it.

**Test risky changes against throwaway state, not production data.** When testing new Discord message formats, buttons, or state-tracking logic, use a disposable state file and a clearly-labeled `[TEST]` synthetic message rather than manipulating real `ebay_state.json` or real customer conversations.

**Investigate empirically before proposing a fix.** When something looks broken, check actual logs, actual raw API responses, and actual state file contents first — more than one "bug" here turned out to be a red herring (e.g. Python buffering stdout so log lines only appeared on process exit, not in real time).

## Known technical gotchas (learned the hard way)

- **Python stdout buffering:** `print()` output won't show in `pm2 logs` in real time unless flushed — don't assume silence means the process is stuck.
- **Discord link-style buttons cannot be colored.** A button with a `url` is locked to Discord's grey/outline style platform-wide. Buttons needing a custom color must trigger bot logic instead of navigating directly.
- **Buttons that do more than navigate need persistent Views**, registered on bot startup with a `custom_id` encoding whatever data they need (e.g. conversation ID), so they keep working after a restart/redeploy — not just while the original process that sent them is still running.
- **eBay's Message API only exposes read/unread status, not reply status.** `unreadCount`/`readStatus` reflect whether a message was *opened*, not whether it was *answered*. "Still needs a reply" must be derived from comparing `latestMessage.senderUsername` against the known buyer — if the buyer is still the last sender, it's unanswered regardless of read state.
- **eBay's Message Center deep link uses `latestMessage.messageId`** as the `question_id` URL parameter — not `conversationId`. Confirmed empirically, not documented anywhere obvious.
- **Correct eBay OAuth scope for messaging is `commerce.message`** (the readonly variant does not exist as a real scope — this caused a real `invalid_scope` error before it was corrected).
- **Conversation-level tracking must dedupe by message, not just by conversation.** Tracking only "have I seen this conversation" misses legitimate new messages arriving on an already-open conversation. Track the last-notified `messageId` per conversation instead.
- **"Mark as Finished"-style manual overrides should clear state surgically, not entirely** — clearing everything about a conversation (including "have I already notified about this message") can cause the same message to be re-announced repeatedly on every subsequent poll.
- **New "have I already handled this" state fields need a one-time backfill for entities already being tracked under the old logic, not just prospective writes going forward.** This has caused real bugs twice in this project (the sender-based tracking migration reposting old messages, and the Finish-button re-announcement bug, where conversations already open in `customer_conversations` at deploy time had no `customer_last_notified_message_ids` entry to protect them). Treat backfill as a required step whenever adding this kind of safeguard field, not an afterthought.
- **Discord embeds never trigger real notifications/pings, even with a `<@user_id>` mention in the description.** Only a mention in the message's `content` field (or an interactive component) counts for notification purposes — confirmed against Discord's official docs. Anything meant to actually ping someone must go in `content=`, not the embed.
- **An individual `<@user_id>` mention in a thread's starter message auto-subscribes that user to the thread for future replies/notifications** — confirmed empirically via `thread.member_count` (jumped from 1 to 2 immediately after posting a root message with just the user mentioned in `content`, before any other message touched the thread). No explicit `thread.add_user()` call needed for that case. (Checking this via `thread.fetch_members()` requires the privileged `GUILD_MEMBERS` intent, which isn't enabled for this app — `member_count` on the thread object doesn't need it and was sufficient to verify.)
- **`@everyone`/`@here` do NOT behave like an individual mention for thread auto-subscription.** Confirmed empirically: `member_count` stayed at 1 (bot only) both immediately after an `@everyone`-only thread starter message *and* after a follow-up `@everyone` sent inside that same thread — it never added anyone. Matches Discord's own support docs: `@everyone`/`@here` in a thread only notify people who are *already* thread members, they never add new members. The root notification posts to a normal (not-yet-a-thread) channel message, so `@everyone` there pings normally — but anything posted *inside* a thread (24h escalations, future replies) needs someone to already be a thread member for `@everyone` to reach them there. **Mitigated:** `add_owner_to_thread()` explicitly calls `thread.add_user()` right after every thread is created (both in `create_new_conversation_thread` and the legacy self-heal path in `get_or_create_conversation_thread`), so the owner is a real thread member from creation onward and `@everyone` inside the thread does reach them. Confirmed via `member_count` jumping 1→2 right after `add_user()`, same signal as the individual-mention case.
- **The bot needs the "Mention @everyone, @here, and All Roles" permission** for `@everyone` in its messages to actually notify anyone — without it, the mention just renders as plain, non-pinging text. Must be granted via the OAuth2 URL Generator's Bot Permissions when inviting/re-inviting the bot (see README Part 1). `thread.add_user()` additionally needs "Send Messages in Threads" (already implied by the bot successfully posting into threads elsewhere in this feature).
- **`guild.members` (cache) and `guild.fetch_members()` (REST) both hard-require the privileged `GUILD_MEMBERS` intent** — confirmed against discord.py's own source (`Guild.fetch_members()` raises `ClientException` client-side if `Intents.members` isn't enabled, before even making the request). This app doesn't have that intent enabled, so `guild.members` only ever contains the bot itself, never other server members — looks like it "worked" (returns a list, no error) but silently returns the wrong thing. **`thread.add_user(discord.Object(id=known_id))` does NOT need this intent** — it's a targeted write with a known snowflake, not a bulk member read, which is why `add_owner_to_thread()` above works without enabling anything in the Developer Portal. Get the "known id" via `client.application_info()` (also intent-free) rather than trying to enumerate members.
- **Deleting a message does not delete its thread.** Discord leaves the thread orphaned and still fully visible in the channel's thread list, unarchived, even though the message that started it is gone (confirmed empirically: fetching the thread by ID succeeded with 404 on the message but a live thread object back). Test cleanup that deletes the root notification message does *not* clean up the thread — `thread.delete()` must be called explicitly on the thread itself.
- **`del state["customer_conversations"][convo_id]`-style resolution (seller replied, "Mark as Finished") throws away the thread pointer along with everything else** — so when the buyer writes again later, the conversation looks brand-new (`entry is None`) and gets a whole new root notification + thread + full history re-backfill, duplicating a thread that's still sitting right there. Caused real duplicate threads in production (one buyer had 4, another had 2) purely from normal back-and-forth (seller replies mid-negotiation, buyer keeps talking). **Fixed** via `customer_conversation_thread_ids: {convo_id: thread_id}` — a separate persistent map (same pattern as `customer_last_notified_message_ids`) that's never cleared by either resolution path, so a later buyer message can reopen the existing thread (`reopen_conversation_thread()`) instead of recreating it. Applies equally to "Mark as Finished" — that handler was checked and needs no separate fix, since it never touched this new dict either, same as the seller-reply path. **Known gap not fixed:** reopening doesn't restore the root notification's embed/button if it was previously marked Finished (stays showing "✅ Marked as finished" with a disabled button even though the thread is active again) — cosmetic/confusing, not a duplicate or crash, flagged but out of scope for this fix.
- **`reopen_conversation_thread()` distinguishes "thread genuinely gone" (`discord.NotFound`) from transient failures (other `discord.HTTPException`, e.g. rate limits/5xx) via a tri-state return** (`True`/`False`/`None`) rather than a plain bool. Only `False` (genuinely gone, or thread found but sending into it failed) triggers the caller's fallback to `create_new_conversation_thread()`; `None` (transient) causes the poll to just skip and retry next cycle. Collapsing these to one boolean would let a momentary API hiccup get mistaken for "thread's gone," creating a duplicate — the exact bug this whole fix exists to prevent, just via a different trigger.

## Current features (status: live in production)

- `/remind`, `/reminders`, `/cancelreminder` — personal reminders, natural-language date/time parsing via `dateparser`
- eBay general activity feed → one channel: new orders, new/updated conversations (including eBay's own system notices, labeled distinctly from real buyer messages)
- eBay customer-message channel → separate channel: one root notification per conversation (buttons: "Reply on eBay" + "Mark Conversation as Finished") with a thread on it holding the full message history, backfilled once on first sighting; new messages on a tracked conversation post as replies in that same thread instead of new top-level notifications; 24h unanswered escalation reminders also post into that thread. The root notification and the 24h escalation both `@everyone`-ping in the message content (embeds can't trigger real Discord notifications, only content/component mentions can — see gotchas). Every newly-created thread also gets the bot owner explicitly added as a member via `thread.add_user()` (see gotchas — `@everyone` inside a thread only reaches existing members, so this is what makes the escalation ping actually work, not just the root notification). If a conversation resolves (seller replies, or gets marked Finished) and the buyer later writes again, it reopens the same existing thread rather than creating a duplicate one — see gotchas on `customer_conversation_thread_ids`.
- Rate-limit handling: eBay API calls are wrapped to detect and gracefully handle rate-limit responses without crashing or spamming alerts

## Roadmap / not yet built

- Cases & disputes tracking (needs new `sell.payment.dispute` scope, separate OAuth consent redo)
- Multi-eBay-account support (needs config restructuring — current design assumes one account throughout)
- Git version control for this project (in progress as of this writing — commit early, commit often, so risky changes are always revertible) (DONE)
- Root notification's embed/button doesn't get restored when a "Finished" conversation reopens (buyer writes again) — thread works fine, but the root message keeps showing "✅ Marked as finished" with a disabled button indefinitely. Cosmetic, deliberately deferred — see the `customer_conversation_thread_ids` gotcha for why.

## Working style for this project

The owner is a non-coder directing development through conversation. Prefer:
- Clear, well-scoped instructions executed fully rather than partial attempts requiring lots of follow-up
- Proactively testing changes (throwaway state, synthetic data) before declaring something done
- Flagging genuine uncertainty (e.g. "I can't verify this without checking X") rather than guessing silently
- Explaining *what* broke and *why* in plain terms when something goes wrong, not just that it's fixed

### Plan before you build

Requests from the owner often arrive as loose, informal descriptions, not fully-specified specs — that's expected, not a gap to push back on. For any non-trivial request:
1. Translate it into a clear, scoped plan before touching files: what will change, which files, what's explicitly out of scope. Surface ambiguities and reasonable assumptions rather than guessing silently or stalling on a clarifying question when a sensible default exists.
2. Use Plan Mode (or otherwise confirm the plan) before implementing, unless the request is genuinely trivial (a one-line fix, a config value).
3. Implement.
4. Before declaring the task done, hand off to the `qa-reviewer` subagent (if configured) to check correctness, then report back in plain terms.

This applies even mid-task — if new instructions arrive that change scope, re-scope out loud before continuing rather than silently absorbing the change.