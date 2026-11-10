# JARVIS Core Assistant Brain

A free, text-based personal assistant with persistent memory, powered by
Google Gemini's free tier.

## Setup

1. Install PostgreSQL locally (or point at any reachable Postgres
   instance) and create a database, e.g. `jarvis`.
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Get a free Gemini API key from https://aistudio.google.com/apikey
4. Copy `.env.example` to `.env`, paste your Gemini key in, and set
   `DATABASE_URL` to your Postgres connection string (e.g.
   `postgresql://postgres:yourpassword@localhost:5432/jarvis`):
   ```
   cp .env.example .env
   ```
5. Run it:
   ```
   python main.py
   ```

Type `exit` or press Ctrl+C to quit. The assistant remembers facts about
you across restarts — stored in Postgres, shared by the CLI, the local
Discord bot, and (if you've set one up) an always-on cloud instance — and
learns new ones roughly every 5 messages, or immediately if you
explicitly say something like "remember that I use VS Code" or "don't
forget my birthday is in May".

For an always-on cloud instance that keeps chatting even when this PC is
off (deferring to this PC's instance whenever it's reachable), see
[docs/deployment.md](docs/deployment.md).

Gemini's free tier caps each model at a small number of requests per day.
Since that cap is per-model, Jarvis automatically falls through an ordered
list of models (strongest first) whenever one runs out for the day —
see `GEMINI_MODELS` in `.env.example` to customize the list.

## Discord Bridge Setup

This lets you talk to the same assistant from Discord (e.g. on your
phone), sharing the same memory as the CLI.

1. Go to https://discord.com/developers/applications and create a
   **New Application** (any name, e.g. "Jarvis").
2. Open the **Bot** tab, click **Reset Token** to reveal it, and copy it
   into `DISCORD_BOT_TOKEN` in `.env`.
3. On the same **Bot** tab, enable **Message Content Intent** under
   Privileged Gateway Intents.
4. Open **OAuth2 > URL Generator**, check the **bot** scope, then under
   Bot Permissions check **View Channels**, **Send Messages**, and
   **Read Message History**. Open the generated URL and invite the bot
   to your own server.
5. On your server, create a private channel just for you and the bot.
6. In Discord, enable **Developer Mode** (User Settings > Advanced),
   then right-click the private channel and **Copy Channel ID** into
   `DISCORD_CHANNEL_ID`, and right-click your own username and
   **Copy User ID** into `DISCORD_OWNER_ID`.
7. Run it:
   ```
   python discord_bot.py
   ```

The bot only responds in that one channel, and only to you — every other
message is silently ignored.

You can also send images (PNG, JPEG, WebP, HEIC/HEIF) up to 7 MB — Jarvis
will look at them using Gemini's vision support and reply based on what's
in the picture.

## PC Automation

Jarvis can open apps, folders, websites, or named multi-step routines on
this PC just by asking naturally — no fixed command syntax needed. For
example: "let's work on trading" or "open my diahealthylife folder."

- If it already knows what you mean, it just does it.
- If it doesn't, it'll guess from context and ask you to confirm before
  doing anything ("I think you mean the folder 'trading_journal' at
  `E:\...` — should I go ahead?"). Once you confirm, it remembers that
  for next time — instantly, no more asking.
- A pending "should I go ahead?" question expires after 24 hours or 3
  messages if you don't answer it, so an old forgotten question can't
  suddenly execute out of context much later.
- Everything it opens goes through safe "open this file/folder/URL"
  calls — never a raw command, so a wrong guess can only open the wrong
  thing, never run something destructive.

This only works on Windows (it uses `os.startfile`). If you're running a
cloud instance on Linux (see [docs/deployment.md](docs/deployment.md)),
PC automation is simply unavailable there — it declines these requests
gracefully instead of crashing, and defers to your local, Windows
instance whenever that instance is reachable.

**Window placement.** A routine can also open specific steps in their
own window and move them to a specific monitor — e.g. "put the
TradingView tab on my first monitor and the journal on my second."
Every step must already be a known action before it can be bundled into
a routine like this (Jarvis will walk you through confirming each one
individually first if they aren't). If a requested monitor doesn't
exist, or the newly opened window can't be found in time, that one
step's placement is silently skipped — it still opens, just without
being moved, so a placement hiccup never blocks the rest of a routine.
By default Jarvis looks for Brave at its standard Windows install
location to force new-window launches; set `BROWSER_EXECUTABLE_PATH` in
`.env` if you're on a different Chromium-based browser or a
non-standard install path.

A step can also be maximized ("open TradingView maximized on my first
monitor") or given a specific size — either a fraction of its monitor
("half the screen") or an exact pixel size. Monitors are numbered the
same way Windows Display Settings numbers them (your primary monitor
is always 1, regardless of where it physically sits).

If you ask for placement on something that's *already* a known action
(e.g. "open TradingView on my second monitor at half size"), Jarvis
just does it immediately — no confirmation, and nothing new gets saved.
Confirmation is only needed the first time an action itself is new;
once it's known, any placement you ask for on it runs right away.

## Memory and Notes

Beyond simple one-line facts, Jarvis keeps richer notes it can find by
meaning, not just by recency. Ask it to "remember this as a note" for
anything more substantial than a single fact, and later ask something
related — it'll search its notes and pull in what's relevant, even if
it's not in the last 20 messages. Conversation that ages out of recent
history isn't simply lost either: once a day, the cloud instance
reviews older messages and distills anything worth keeping into notes
automatically.

### Profile (About me)

Jarvis keeps one organized **About-me document** — who you are, goals,
projects, typical week, people, setup — and every brain reads it on every
message. It keeps it up to date by itself: when you mention something lasting
("I moved boxing to 07:00", "I started a Spanish course"), it edits just that
line and adds a short note under its reply, e.g.
`📝 Profile: boxing 06:00 → boxing 07:00`. Every 5 messages it also double-checks
the recent chat for anything it missed. One-off moods ("I'm tired today") are
not saved, and after reading web content Claude has to ask before changing it.

- `profile` — show the document (attached as `profile.md` when long).
- `profile undo` — revert the last change (send it again to redo).
- To fix something, just say it: "remove X from my profile", "my workout is at 07:00 now".

It starts empty; tell Jarvis about yourself and it fills the profile in.

### Lessons

Lessons are short rules about how **Jarvis** should behave (the profile is about
you). When you correct it — "no, I didn't give a time", "you only added one",
"wrong folder" — it saves a lesson right away and shows a note under its reply,
e.g. `🧠 Lesson: Never invent a time for a reminder.` Every brain follows the
lessons on every message. Every 5 messages it also double-checks your recent
messages for corrections it missed. Up to 40 lessons; after Claude has read web
content it has to ask before saving one.

- `lessons` — the numbered list.
- `lessons undo` — revert the last change (send again to revert the one before).
- `lessons forget 3` — remove lesson 3.

## Reminders and daily to-do list

Ask Jarvis in chat: "remind me at 21:00 to close my trades", "every
weekday at 9 remind me to check the market", "add the gym to my list",
"what's on Friday?", "done with the gym".

- Reminders ping you on Discord with **Done / Snooze 10m / Snooze 1h**
  buttons, even when your PC is off (the cloud instance runs the
  scheduler).
- Every day Jarvis posts and pins your to-do list, including today's
  reminders, no later than `TODO_LIST_TIME` (default 09:00, in
  `JARVIS_TIMEZONE`) — but if your first timed reminder that day is
  earlier than that, it posts `TODO_LIST_LEAD_MINUTES` (default 30)
  minutes before it instead, so the list is up before you're pinged.
  Tap an item's button to tick it off; unfinished to-dos carry over.
- A ping you don't answer is re-sent every hour (up to 3 more times), and at
  `NUDGE_TIME` (default 19:30) Jarvis nudges you about undone untimed
  reminders and to-dos. (Marking one occurrence of a several-times-a-day
  reminder done stops that day's re-pings for all of it.)
- Firing reminders and posting the list use no Gemini quota.
- Every reminder and to-do has a category (birthdays, subscriptions,
  supplements, fitness, trading, learning, content, admin, tasks, or
  other). When the list spans more than one category, the pinned list
  and `list_agenda` group items under a header for each; a single-category
  day still renders as a plain numbered list.
- Ask for an early reminder too: "remind me every year for Giorgi's
  birthday, and the day before as well" adds a linked reminder that fires
  N days before each occurrence, worded "Tomorrow: …" (or "In N days: …").
  Cancelling the main reminder cancels its early one too; marking it done
  does not.
- Some things don't need a ping — "add a daily stretch to my list, no need
  to ping me" creates an untimed reminder: it sits on the daily list with
  no ⏰ and never fires, but still repeats and can be ticked off like any
  other item.
- Change an existing reminder in chat: "move Duolingo to 21:00", "make
  Mom's birthday reminder repeat every year instead", "stop pinging me for
  the gym, just put it on my list." Jarvis finds it by name or list id and
  confirms the exact new date/time (or "no ping") before moving on.

## Brains (Groq, Gemini, Claude)

Every message is judged to pick the best AI brain: **Groq** (free, instant)
handles pure small talk; **Gemini** (free tier) handles everyday tasks and
reminders; **Claude Sonnet** (via your Claude Pro subscription, no extra
cost) handles complex reasoning like planning, analysis, writing, and coding.
When unsure, Jarvis routes up, never down. Photos go to Gemini first (Claude
doesn't support images here yet).

**Direct overrides** in your message force a specific brain:
- "use claude", "ask claude", "think hard", "think carefully" → Claude
- "use gemini" → Gemini

**Fallback:** If Claude hits its daily limit, the answer comes from Gemini
instead. If Gemini maxes out, Claude tries next, then Groq for text-only
answers. Each reply shows a tiny tag like `— sonnet` or `— gemini (Claude
limit, resets 15:45)` so you know which brain answered — hide it with
`JARVIS_SHOW_BRAIN=false` in `.env`.

**Safety:** In normal chat Claude only has Jarvis's own tools (reminders,
to-do, notes, web, weather, automation). It won't open or run things on your
PC in the same answer where it read web content or stored memories. File and
command access is a separate, guarded mode — see "Computer access" below.

**Setup:**
- `GROQ_API_KEY` (free at https://console.groq.com/)
- `CLAUDE_CODE_OAUTH_TOKEN` (run `claude setup-token` for a one-year token)
- Keep `ANTHROPIC_API_KEY` unset — Claude uses your Pro subscription instead
- Without a Groq key, everything routes to Gemini; without a Claude token,
  hard questions fall back to Gemini.

**Personal use:** Claude draws from your own Claude Pro subscription for
personal use on your own machines. If Jarvis is ever offered to other people,
Claude access must move to a paid API key.

## Computer access

Ask Jarvis in Discord to work with your files, programs and commands — "sort
my Downloads into folders", "how much disk is free", "install VLC", "open my
VoiceAssistant project in VS Code". Requests like these go to Claude, which
works on your PC through a safety guard. Chat stays in Discord.

**What runs on its own, what asks, what's refused**

| | Examples |
|---|---|
| Runs on its own | Reading normal files, listing and searching, look-only commands (`dir`, `git status`, disk usage), creating folders and new files, editing files, copying, opening documents |
| Asks unless `/bypass` is on | Moving or renaming, moving to the Recycle Bin, installing or uninstalling, overwriting a file |
| Always asks, even in bypass | Any other terminal command (the card shows the exact command), sending a file off the PC, reading secret files (`.env`, keys, SSH, browser and wallet data), writing to places that make code run later (Startup, shell/PowerShell profiles, git hooks, Claude Code settings), opening programs or downloaded files, every command on the cloud server |
| Always refused | Formatting disks or partitions, boot settings, deleting a drive/your user folder/system folders, registry outside your apps, download-and-run, turning off security, accounts and permissions, anything that would change Jarvis itself (its folder, `.env`, scheduled task, service, database, Python packages) |

An approval card shows what Claude wants to do, why, and `[Allow]` / `[Deny]`
buttons. Only you can press them; no answer in 10 minutes counts as Deny.

**Deleting** never deletes for good: files go to the **Recycle Bin**, so you
can restore them from there. Drives without a Recycle Bin (USB, network) and
anything over 2 GB are refused instead.

**After web content:** once Claude has read something from the internet or a
downloaded file in a task, bypass pauses for the rest of that task and every
risky step asks again (protection against instructions hidden in web pages).

**Chat commands** (owner only, no AI involved). Send the word as the whole
message — Discord keeps a leading `/` for its own slash commands, so the slash
is optional (`bypass`, `!bypass` and `/bypass` all work):
- `bypass` — stop asking for the "asks unless bypass" steps for 12 hours
  (`BYPASS_HOURS`), on the PC only. You get a warning 15 minutes before it
  ends.
- `safe` — end bypass now.
- `queue` — PC tasks waiting for the PC; `cancel <n>` removes one.
- `log [n]` — the last n (default 15) things Claude did, with each decision.

**PC off:** if you ask for PC work while the PC is off, the cloud Jarvis queues
it and the PC runs it when it starts (tasks older than 7 days are dropped).
Queued tasks always ask before risky steps, even in bypass.

**Long tasks** show a progress line with a **Stop** button. Limits: 60 steps or
30 minutes per task (`COMPUTER_TASK_MAX_STEPS`, `COMPUTER_TASK_TIMEOUT_MINUTES`)
— say "continue" to finish. Long outputs come as `output.txt`, file changes as
`changes.diff` (secret files are never included).

**Server:** on the cloud server Claude can run commands too, but every command
asks and bypass never applies there.
