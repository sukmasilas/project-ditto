# Discord Reminder Bot — Setup Guide

This is your own custom Discord bot. Right now it does one thing: lets anyone in your
server type `/remind`, `/reminders`, or `/cancelreminder` to manage reminders that get
posted back to the channel when they're due. It's yours to extend — every future
feature just gets added to `bot.py`.

Two parts to set up: (1) create the bot on Discord's side, (2) put the code online so
it runs 24/7. Neither requires writing code — just following steps.

---

## Part 1 — Create the bot in Discord

1. Go to https://discord.com/developers/applications and log in with your Discord account.
2. Click **New Application**, give it a name (e.g. "Reminder Bot"), accept the terms, click **Create**.
3. In the left sidebar, click **Bot**.
4. Click **Reset Token** (or **Copy** if a token is already shown) and save it somewhere
   safe — this is the `DISCORD_BOT_TOKEN` you'll need in Part 2. Treat it like a password;
   anyone with it can control your bot.
5. Scroll down on the same page and make sure **Public Bot** is toggled based on your
   preference (off is fine if only you will invite it). You do **not** need to enable
   "Message Content Intent" — this bot only uses slash commands.
6. In the left sidebar, click **OAuth2** → **URL Generator**.
   - Under **Scopes**, check `bot` and `applications.commands`.
   - Under **Bot Permissions**, check `Send Messages`, `Read Message History`, `Use Slash Commands`,
     `Mention @everyone, @here, and All Roles` (needed for the eBay customer-message channel's
     @everyone pings to actually notify anyone, not just render as plain text), and `Manage Threads`
     (needed to properly delete threads — without it, deleting the root message leaves the thread
     itself orphaned and still visible).
   - Copy the generated URL at the bottom, paste it into your browser, choose your
     server, and click **Authorize**. Your bot now appears in the server (offline until
     Part 2 is done).

---

## Part 2 — Get the code running 24/7 (Railway, free tier)

You'll upload the code to GitHub first (so Railway can pull it), then connect Railway to it.

### Step A: Put the files on GitHub

1. Create a free account at https://github.com if you don't have one.
2. Click the **+** icon (top right) → **New repository**. Name it e.g. `discord-reminder-bot`,
   keep it **Private**, click **Create repository**.
3. On the new repo page, click **uploading an existing file**.
4. Drag in these files: `bot.py`, `requirements.txt`, `Procfile`. (Do **not** upload
   `.env.example` with a real token in it — you'll set the token directly in Railway instead,
   which is safer.)
5. Click **Commit changes**.

### Step B: Deploy on Railway

1. Create a free account at https://railway.app (you can sign up with GitHub directly).
2. Click **New Project** → **Deploy from GitHub repo** → select the `discord-reminder-bot`
   repo you just created. Authorize Railway to access it if prompted.
3. Railway will detect it's a Python app automatically. Before it finishes deploying, go to
   the project's **Variables** tab and add:
   - `DISCORD_BOT_TOKEN` = (the token you copied in Part 1)
   - `BOT_TIMEZONE` = your timezone as an IANA name, e.g. `Asia/Jakarta` (default already
     set in the code if you skip this)
4. Go to the **Settings** tab → under **Deploy**, make sure the start command is
   `python bot.py` (it should pick this up automatically from the `Procfile`).
5. Railway will build and start the bot. Check the **Deployments** → **Logs** tab —
   you should see `Logged in as [YourBot] — reminder bot is ready.`
6. Go back to your Discord server — the bot should now show as online, and typing `/remind`
   in any channel should bring up the command.

Railway's free tier includes monthly usage credit that comfortably covers a small bot
like this running continuously. If you ever exceed it, Railway will notify you before
anything is interrupted.

---

## How to use it in Discord

- `/remind when:tomorrow at 3pm message:Call the plumber`
  → Bot confirms, then posts a reminder mentioning you in that channel at that time.
- `/remind when:in 2 hours message:Check the oven`
- `/remind when:Aug 10 2pm message:Submit report`
- `/reminders` → Lists your own upcoming reminders with their ID numbers.
- `/cancelreminder reminder_id:3` → Cancels reminder #3 (only the person who set it can cancel it).

The `when` field accepts natural language ("tomorrow at 3pm", "in 2 hours", "Aug 10 2pm", etc.),
interpreted relative to the `BOT_TIMEZONE` you set. If the bot can't understand what you typed,
it'll ask you to rephrase.

---

## Adding more features later

Everything lives in `bot.py`. Each command is a small, self-contained block starting
with `@tree.command(...)`. When you want a new feature (moderation, eBay alerts, Drive
notifications, etc.), describe it and I'll write the new command, you paste it into
`bot.py` on GitHub (or I can walk you through it), commit the change, and Railway
automatically redeploys with the update — no need to repeat the setup steps above.
