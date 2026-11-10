# Cloud deployment (always-on Oracle Cloud VM)

This documents how the cloud instance of Jarvis was set up, so the VM can be
rebuilt from scratch if it's ever lost. The cloud instance runs the bot
24/7 for chat/memory; it defers to the local instance (see `main.py` /
`discord_bot.py`'s `instance_role` handling) whenever the local PC is on,
via the `local_heartbeat` liveness check in `brain/memory.py`.

## 1. Oracle Cloud account and VM

1. Create an Oracle Cloud account at https://www.oracle.com/cloud/free/.
   Identity verification requires a card, but "Always Free" resources are
   never charged. Pick a **Home Region** close to you — it cannot be
   changed later. This deployment used **me-dubai-1**.
2. **Networking** (create this before the instance, so the instance
   wizard's public-IP toggle is actually selectable):
   - Networking → Virtual Cloud Networks → **Create VCN**. In this
     console version, the plain "Create VCN" dialog does *not* offer a
     "with internet connectivity" wizard template — it only creates a
     bare VCN. IPv4 CIDR block used: `10.0.0.0/16`.
   - Inside the new VCN, **Gateways** tab → **Create Internet Gateway**.
   - **Routing** tab → open the VCN's default route table → **Route
     Rules** → **Add Route Rules**: target type Internet Gateway,
     destination `0.0.0.0/0`, target = the gateway just created.
   - **Subnets** tab → **Create Subnet**: regional, CIDR `10.0.0.0/24`,
     route table = the default one (now has the internet gateway rule),
     subnet access = **Public Subnet**. Leave the default security list.
3. **Compute → Instances → Create instance**:
   - Image: **Canonical Ubuntu** (newest LTS — 26.04 at the time of this
     deployment).
   - Shape: **Ampere / VM.Standard.A1.Flex**, sized to stay within the
     "Always Free eligible" OCPU/memory allowance shown by the console
     (fall back to **VM.Standard.E2.1.Micro** if Ampere capacity isn't
     available in the region).
   - Networking: select the VCN/public subnet created above, and turn on
     **Automatically assign public IPv4 address**.
   - SSH keys: "Generate a key pair for me", download and keep the
     private key — it's the only copy Oracle gives you.
4. **Security list rule for Postgres**: on the subnet's security list,
   add an ingress rule — Source CIDR = *your home IP*/32 (not
   `0.0.0.0/0`), protocol TCP, destination port 5432. Home ISPs don't
   guarantee a fixed IP; if it ever changes, this rule (and the VM's own
   firewall rule below) both need updating, or Postgres access from your
   PC silently stops working.

## 2. PostgreSQL on the VM

```bash
sudo apt update
sudo apt install -y postgresql
sudo -u postgres psql -c "ALTER USER postgres PASSWORD '<generated>';"
```

Allow remote connections from your home IP only:
- `/etc/postgresql/18/main/postgresql.conf`: `listen_addresses = '*'`
- `/etc/postgresql/18/main/pg_hba.conf`, appended:
  ```
  host    jarvis          postgres        <home-ip>/32       scram-sha-256
  ```
- `sudo systemctl restart postgresql`
- `sudo -u postgres psql -c "CREATE DATABASE jarvis;"`

The VM's own OS-level firewall (`iptables`, independent of the OCI
security list above) only allows SSH by default. Add and persist a rule
for Postgres from the same home IP:

```bash
sudo iptables -I INPUT 4 -p tcp -s <home-ip>/32 --dport 5432 -j ACCEPT
sudo netfilter-persistent save
```

(Rule position 4 was chosen to land before the catch-all REJECT rule —
check `sudo iptables -L INPUT -n --line-numbers` and adjust the position
if the existing rule set differs.)

## pgvector (for semantic memory/notes)

Install the extension matching your Postgres major version:

```bash
sudo apt install -y postgresql-18-pgvector
sudo -u postgres psql -d jarvis -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Local Windows Postgres does not have a pgvector package available
(confirmed absent from Stack Builder's catalog as of Postgres 18) —
this only affects local testing, not production, since both instances
already share the VM's Postgres. See `.env.example`'s
`NOTES_TEST_DATABASE_URL` for how the test suite handles this.

## 3. Deploy the bot code

The VM needs Python 3.14+ and a venv (Ubuntu's `python3-venv` package is
required for `ensurepip` to work):

```bash
sudo apt install -y python3.14-venv
cd ~/jarvis-voice-assistant
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

`requirements.txt` marks `pywin32` as Windows-only
(`pywin32; sys_platform == "win32"`), so `pip` skips it automatically on
Linux — no special-casing needed.

Computer access (Recycle-Bin deletes) adds `send2trash`, which installs from
`requirements.txt` like everything else. The server needs nothing more: on
the cloud instance every Claude command asks for approval, the file tools are
off, and the new tables (`computer_state`, `pc_tasks`, `activity_log`) are
created automatically at start.

**Getting the code onto the VM:** at the time of this deployment, the
Postgres/cloud-deployment feature was still on an unmerged branch, not
yet on GitHub's `main`. It was deployed directly from the working branch
via `git archive HEAD | ssh <vm> "tar -x -C ~/jarvis-voice-assistant"`
(tracked files only, no `.git`). **Once this branch merges to `main`,
redeploy by cloning/pulling from GitHub instead** —
`git clone git@github.com:Kashxalaa/jarvis-voice-assistant.git` (repo is
private; the VM needs its own deploy key or a token) for a fresh VM, or
`git pull && sudo systemctl restart jarvis` on this VM to pick up future
changes.

## 4. `.env`

Create `~/jarvis-voice-assistant/.env` (`chmod 600`, never commit):

```
GEMINI_API_KEY=<same value as local .env>
DISCORD_BOT_TOKEN=<same value as local .env>
DISCORD_CHANNEL_ID=<same value as local .env>
DISCORD_OWNER_ID=<same value as local .env>
DATABASE_URL=postgresql://postgres:<the VM's own Postgres password>@localhost:5432/jarvis
JARVIS_ROLE=cloud
EXA_API_KEY=<same value as local .env, if you set one — see .env.example>
```

`DATABASE_URL` points at `localhost` here since Postgres and the bot run
on the same VM — no need to route through the public IP for this
connection (that's only needed from your PC). `EXA_API_KEY` is optional
(enables the `web_search` tool) — set it here too if you set it locally,
or the cloud instance simply won't offer web search.

## 5. Migrate real data

Run once, from whichever side can already reach the other (in this
deployment, run from the local PC pointed at the VM's public IP, since
the local PC is where `assistant_memory.db` actually lives):

```bash
DATABASE_URL=postgresql://postgres:<vm-password>@<vm-public-ip>:5432/jarvis \
  python scripts/migrate_to_postgres.py
```

`pending_proposal` is deliberately not migrated (transient state).

## 6. systemd service

`/etc/systemd/system/jarvis.service`:

```ini
[Unit]
Description=Jarvis Discord bot (cloud instance)
After=network.target postgresql.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/jarvis-voice-assistant
ExecStart=/home/ubuntu/jarvis-voice-assistant/venv/bin/python3 discord_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable jarvis
sudo systemctl start jarvis
sudo systemctl status jarvis   # expect: active (running)
```

`ExecStart` points at the venv's own `python3` (not the system one) so it
picks up the installed dependencies without needing the venv activated.

## Redeploying after code changes

```bash
# from the local PC, after committing/pushing the change:
ssh <vm> "cd ~/jarvis-voice-assistant && git pull && ./venv/bin/pip install -r requirements.txt && sudo systemctl restart jarvis"
```

(Until this branch merges to `main`, substitute the `git archive | ssh ... tar -x` step from section 3 instead of `git pull`.)

Optional `.env` settings on the VM, added with later sub-projects:
`EXA_API_KEY` (enables web_search), `JARVIS_TIMEZONE` (default
`Asia/Tbilisi`), `TODO_LIST_TIME` (default `09:00`, the latest time the
daily to-do list is posted), and `TODO_LIST_LEAD_MINUTES` (default `30`
— the list posts this many minutes before your first timed reminder of
the day if that's earlier than `TODO_LIST_TIME`). Only the cloud
instance runs the reminder scheduler; the reminder tables are created
automatically on startup.

Update the cloud VM and restart the local instance together — an older
local instance with a fresh heartbeat makes the cloud stay silent on
clicks it can't handle.

### Brain router (Groq + Claude)

On Windows the Agent SDK can't use the npm `claude.CMD` shim; Jarvis auto-detects the native `claude.exe` (override with `CLAUDE_CLI_PATH`).

The Claude Code CLI must be installed separately on the VM (the Agent SDK
wheel doesn't bundle it):

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

The installer places the binary in `~/.local/bin`. Ensure the systemd
service's `PATH` includes it. Edit `/etc/systemd/system/jarvis.service` to
add an `Environment` line to the `[Service]` section:

```ini
[Service]
...
Environment=PATH=/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin
...
```

Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart jarvis
```

Verify the CLI version supports `--tools`, `--setting-sources`,
`--strict-mcp-config` and `--no-session-persistence`:

```bash
~/.local/bin/claude --version
```

Add the following to the VM `.env`:

```
GROQ_API_KEY=<from https://console.groq.com/>
CLAUDE_CODE_OAUTH_TOKEN=<from 'claude setup-token', valid one year>
```

Ensure `ANTHROPIC_API_KEY` is **not set** (Claude uses your Pro subscription
instead). Install the additional dependencies:

```bash
./venv/bin/pip install -r requirements.txt
```

Then restart the service. **Also update the local PC bot at the same time**
— a fresh cloud instance with an older local instance can miss messages it
can't handle.

## 7. Local auto-start (Windows Task Scheduler)

```powershell
$action = New-ScheduledTaskAction -Execute "C:\Users\alex\AppData\Local\Python\bin\python.exe" -Argument "discord_bot.py" -WorkingDirectory "E:\Programing\VoiceAssistant"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "Jarvis Discord Bot" -Action $action -Trigger $trigger -Settings $settings -Description "Launches the local Jarvis instance (full PC automation) on login."
```

This registers a task that launches `discord_bot.py` at Windows login,
restarting up to 3 times (1 minute apart) if it crashes. It requires an
**elevated** PowerShell session to register (`Register-ScheduledTask`
returns "Access is denied" otherwise) — run it once as Administrator;
the task itself runs as the normal logged-in user afterward.

**Path note:** at the time this was registered, the always-on-cloud
feature branch hadn't merged to `main` yet, so `WorkingDirectory` was
pointed at the branch's worktree
(`E:\Programing\VoiceAssistant\.worktrees\always-on-cloud`) instead of
the path above, for live verification. **After merging, re-point it**:

```powershell
Set-ScheduledTask -TaskName "Jarvis Discord Bot" -Action (New-ScheduledTaskAction -Execute "C:\Users\alex\AppData\Local\Python\bin\python.exe" -Argument "discord_bot.py" -WorkingDirectory "E:\Programing\VoiceAssistant")
```

The local `.env` needs `DATABASE_URL` pointed at the VM's public IP
(port 5432, open to this machine's home IP per section 1) and either
`JARVIS_ROLE=local` or no `JARVIS_ROLE` line at all (`"local"` is the
default) — never `cloud`.

## Verified (2026-09-21)

End-to-end live verification, with the local instance running from the
feature branch's worktree and the cloud instance running as the
`jarvis` systemd service on the Oracle VM, both pointed at the same
production Postgres database:

- **Single responder**: with both instances live, a Discord message got
  exactly one reply (the local instance's) — the cloud instance's
  heartbeat check correctly stayed silent.
- **Heartbeat handoff**: stopping the local instance and waiting past
  the ~30s freshness window, the cloud instance started answering.
  Restarting the local instance and waiting ~12s for its next heartbeat
  write, it resumed answering (confirmed via the scheduled-task's
  startup path too, not just a manual run).
- **Graceful PC-automation decline (cloud)**: asking the cloud instance
  to open a local folder produced a friendly explanation that it can't
  control the local desktop from the cloud, with no crash and no
  silent non-reply — Gemini was simply never offered the automation
  tools for that turn.
- **Graceful `webbrowser.open` failure (cloud, headless Linux)**: with a
  proposal left pending by the local instance before it was stopped,
  confirming it from the cloud instance hit the real
  `webbrowser.open` failure path on the headless VM and returned
  `"I tried to save and open that but ran into a problem: couldn't
  open '<name>': failed to open url: <url>"` — no hang, no crash.
- **Real local automation still works**: after the local instance
  resumed, proposing and confirming a new `app` action (Notepad)
  actually opened it via `os.startfile`, confirming the platform gate
  only affects the non-Windows side.
- **Shared memory across instances**: telling the cloud instance "remember
  that I use VS Code" wrote `code_editor = VS Code` into the shared
  `facts` table (verified directly via `psql`); after switching back to
  the local instance, asking what editor is used correctly recalled it
  — one continuous memory across both instances, as designed.
