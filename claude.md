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

## Current features (status: live in production)

- `/remind`, `/reminders`, `/cancelreminder` — personal reminders, natural-language date/time parsing via `dateparser`
- eBay general activity feed → one channel: new orders, new/updated conversations (including eBay's own system notices, labeled distinctly from real buyer messages)
- eBay customer-message channel → separate channel: buyer messages only, with "Reply on eBay" + "Mark Conversation as Finished" buttons, 24h unanswered escalation reminders (posted as threads on the original message, plain text, no duplicate buttons)
- Rate-limit handling: eBay API calls are wrapped to detect and gracefully handle rate-limit responses without crashing or spamming alerts

## Roadmap / not yet built

- Cases & disputes tracking (needs new `sell.payment.dispute` scope, separate OAuth consent redo)
- Multi-eBay-account support (needs config restructuring — current design assumes one account throughout)
- Git version control for this project (in progress as of this writing — commit early, commit often, so risky changes are always revertible)

## Working style for this project

The owner is a non-coder directing development through conversation. Prefer:
- Clear, well-scoped instructions executed fully rather than partial attempts requiring lots of follow-up
- Proactively testing changes (throwaway state, synthetic data) before declaring something done
- Flagging genuine uncertainty (e.g. "I can't verify this without checking X") rather than guessing silently
- Explaining *what* broke and *why* in plain terms when something goes wrong, not just that it's fixed