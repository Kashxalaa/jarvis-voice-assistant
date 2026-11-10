import asyncio
import io
import logging
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import discord
import psycopg

from brain.approvals import (
    APPROVAL_TIMEOUT_SECONDS,
    ApprovalRequest,
    ApprovalResult,
    PendingApprovals,
)
from brain.brains.claude import ClaudeBrain, ComputerConfig
from brain.brains.gemini import GeminiBrain
from brain.brains.groq import GroqBrain
from brain.computer_state import (
    claim_bypass_warning,
    drop_expired_pc_tasks,
    end_expired_bypass,
    fail_stale_pc_tasks,
    finish_pc_task,
    get_bypass_until,
    next_pc_task,
    prune_activity,
    queue_pc_task,
)
from brain.computer_tools import ComputerTaskState
from brain.brains.claude import _WEB_TAINT_TOOLS
from brain.conversation import Brains, handle_command, is_command_word, run_turn
from brain.daily_list import (
    NUDGE_TITLE,
    ButtonSpec,
    ListMessageMissing,
    ListRender,
    PingCard,
    PingMessageMissing,
    active_ping_card,
    apply_button,
    reping_card,
    run_scheduler_tick,
)
from brain.gemini_client import GeminiClient
from brain.memory import (
    add_message,
    ensure_connected,
    init_db,
    is_local_heartbeat_fresh,
    touch_local_heartbeat,
)
from brain.notes import CONSOLIDATION_INTERVAL_SECONDS, consolidate_old_messages
from brain.reminders import ItemNotFoundError
from config import (
    resolve_api_key,
    resolve_claude_cli_path,
    resolve_claude_model,
    resolve_computer_task_max_steps,
    resolve_computer_task_timeout_minutes,
    resolve_claude_oauth_token,
    resolve_database_url,
    resolve_groq_api_key,
    resolve_groq_model,
    resolve_instance_role,
    resolve_models,
    resolve_show_brain,
    resolve_timezone,
    resolve_todo_list_lead_minutes,
    resolve_nudge_time,
    resolve_todo_list_time,
)

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
SCHEDULER_INTERVAL_SECONDS = 30
BUTTON_TEMPLATE = (
    r"(?P<custom_id>rem:(?:done|s10|s60):\d+|list:[tr]\d+|appr:[0-9a-f]{32}:(?:allow|deny)|stop:[0-9a-f]{12})"
)
APPROVAL_INLINE_LIMIT = 900
APPROVAL_EMBED_COLOR = 0xFEE75C
LIST_EMBED_COLOR = 0x5865F2
BYPASS_WARNING_LEAD = timedelta(minutes=15)
QUEUE_POLL_SECONDS = 30
PROGRESS_MIN_STEPS = 3
PROGRESS_MIN_SECONDS = 5.0
MAX_SEND_FILE_BYTES = 24 * 1024 * 1024
MAX_FILES_PER_MESSAGE = 10
MAX_BATCH_BYTES = 24 * 1024 * 1024


class JarvisButton(discord.ui.DynamicItem[discord.ui.Button], template=BUTTON_TEMPLATE):
    # Persistent: the meaning lives in the custom_id, so buttons on old
    # messages keep working after a restart or redeploy.
    def __init__(self, custom_id: str, label: str = "…",
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=custom_id))
        self.jarvis_custom_id = custom_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["custom_id"], label=item.label or "…", style=item.style)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.handle_button(interaction, self.jarvis_custom_id)


def reminder_view(reminder_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(JarvisButton(f"rem:done:{reminder_id}", "Done", discord.ButtonStyle.success))
    view.add_item(JarvisButton(f"rem:s10:{reminder_id}", "Snooze 10m"))
    view.add_item(JarvisButton(f"rem:s60:{reminder_id}", "Snooze 1h"))
    return view


def list_view(buttons: list[ButtonSpec]) -> discord.ui.View | None:
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)
    for spec in buttons:
        style = discord.ButtonStyle.success if spec.done else discord.ButtonStyle.secondary
        view.add_item(JarvisButton(spec.custom_id, spec.label, style))
    return view


def list_embed(render: ListRender) -> discord.Embed:
    embed = discord.Embed(title=render.title, description=render.body, color=LIST_EMBED_COLOR)
    if render.footer:
        embed.set_footer(text=render.footer)
    return embed


def ping_embed(card: PingCard) -> discord.Embed:
    return discord.Embed(title=card.title, description=card.description, color=card.color)


def approval_view(approval_id: str, batch_size: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    allow_label = f"Allow all {batch_size}" if batch_size > 1 else "Allow"
    view.add_item(JarvisButton(f"appr:{approval_id}:allow", allow_label, discord.ButtonStyle.success))
    view.add_item(JarvisButton(f"appr:{approval_id}:deny", "Deny", discord.ButtonStyle.danger))
    return view


def approval_embed(req: ApprovalRequest, attached: bool) -> discord.Embed:
    embed = discord.Embed(title=f"🔐 Claude wants to {req.summary}"[:256], color=APPROVAL_EMBED_COLOR)
    embed.add_field(name="Machine", value="Server" if req.machine == "server" else "PC")
    if req.reason:
        embed.add_field(name="Why", value=req.reason[:1024], inline=False)
    if attached:
        command = "(too long — see the attached details.txt)"
    else:
        command = "```\n" + req.detail.replace("```", "'''") + "\n```"
    embed.add_field(name="Command", value=command, inline=False)
    return embed


def approval_outcome_embed(title: str) -> discord.Embed:
    return discord.Embed(title=title, color=APPROVAL_EMBED_COLOR)


def progress_view(task_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(JarvisButton(f"stop:{task_id}", "Stop", discord.ButtonStyle.danger))
    return view


class ProgressLine:
    """One message per computer task: posted on the first progress call, edited
    in place after, with a Stop button until finish() removes it."""

    def __init__(self, sender: "DiscordSender", state: ComputerTaskState):
        self._sender = sender
        self.state = state
        self._message = None
        self._finished = False
        self._calls = 0
        self._started = time.monotonic()

    async def update(self, text: str) -> None:
        if self._finished:
            return
        self._calls += 1
        view = progress_view(self.state.task_id)
        if self._message is None:
            # Short turns (a reminder, a quick lookup) get no progress line at all:
            # only from the 3rd step, or once 5 s have passed.
            if (
                self._calls < PROGRESS_MIN_STEPS
                and time.monotonic() - self._started < PROGRESS_MIN_SECONDS
            ):
                return
            channel = await self._sender._channel()
            self._message = await channel.send(text, view=view)
        else:
            await self._message.edit(content=text, view=view)

    async def finish(self) -> None:
        self._finished = True
        if self._message is None:
            return
        label = "🛑 Stopped" if self.state.stopped else "✅ Finished"
        try:
            await self._message.edit(content=f"{label} ({self.state.steps} steps)", view=None)
        except Exception as exc:
            logger.warning("couldn't close the progress line: %s", exc)


def _connect(database_url: str):
    # Per-step connection for the brain's hook and the queue tool: plain
    # autocommit, no schema setup (init_db re-runs every CREATE/ALTER). The callers
    # (get_bypass_until, log_activity, queue_pc_task) are single statements.
    return psycopg.connect(database_url, autocommit=True)


def _discord_files(attachments: list[tuple[str, bytes]]) -> list[discord.File]:
    return [discord.File(io.BytesIO(data), filename=name) for name, data in attachments]


def _batch_attachments(attachments: list[tuple[str, bytes]]) -> list[list[tuple[str, bytes]]]:
    # At most 10 files and under MAX_BATCH_BYTES per message (a single bigger file goes alone).
    batches: list[list[tuple[str, bytes]]] = []
    current: list[tuple[str, bytes]] = []
    size = 0
    for item in attachments:
        if current and (len(current) >= MAX_FILES_PER_MESSAGE or size + len(item[1]) >= MAX_BATCH_BYTES):
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += len(item[1])
    if current:
        batches.append(current)
    return batches


async def send_attachments(channel, attachments: list[tuple[str, bytes]]) -> None:
    for batch in _batch_attachments(attachments):
        try:
            await channel.send(files=_discord_files(batch))
        except discord.DiscordException as exc:
            logger.warning("failed to send attachments: %s", exc)


class DiscordApprovalBroker:
    """Posts approval cards and waits for the owner's button press. The
    registry is in-memory: only the instance that created a card can resolve it."""

    def __init__(self, client: discord.Client, channel_id: int, owner_id: int,
                 pending: PendingApprovals):
        self._sender = DiscordSender(client, channel_id, owner_id)
        self._owner_id = owner_id
        self._pending = pending

    async def request(
        self, req: ApprovalRequest, timeout_s: float = APPROVAL_TIMEOUT_SECONDS
    ) -> ApprovalResult:
        approval_id, future = self._pending.create()
        message = None
        try:
            try:
                attached = len(req.detail) > APPROVAL_INLINE_LIMIT
                file = (
                    discord.File(io.BytesIO(req.detail.encode("utf-8")), filename="details.txt")
                    if attached else None
                )
                channel = await self._sender._channel()
                message = await channel.send(
                    f"<@{self._owner_id}>", embed=approval_embed(req, attached),
                    view=approval_view(approval_id, req.batch_size), file=file,
                )
            except Exception as exc:
                logger.warning("failed to post approval card: %s", exc)
                return ApprovalResult(False, "error", None)
            try:
                allowed, approver = await asyncio.wait_for(future, timeout_s)
            except asyncio.TimeoutError:
                self._pending.discard(approval_id)
                await self._mark_card(message, "⏱ Timed out — denied")
                return ApprovalResult(False, "timeout", None)
            return ApprovalResult(allowed, "allowed" if allowed else "denied", approver)
        except asyncio.CancelledError:
            self._pending.discard(approval_id)
            if message is not None:
                await self._mark_card(message, "Cancelled")
            raise
        except Exception as exc:
            logger.warning("approval request failed: %s", exc)
            return ApprovalResult(False, "error", None)
        finally:
            self._pending.discard(approval_id)

    @staticmethod
    async def _mark_card(message, title: str) -> None:
        try:
            await message.edit(embed=approval_outcome_embed(title), view=None)
        except Exception as exc:
            logger.warning("couldn't update the approval card (%s): %s", title, exc)


class DiscordSender:
    def __init__(self, client: discord.Client, channel_id: int, owner_id: int):
        self._client = client
        self._channel_id = channel_id
        self._owner_id = owner_id

    async def _channel(self):
        channel = self._client.get_channel(self._channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(self._channel_id)
        return channel

    async def send_text(self, text: str) -> None:
        channel = await self._channel()
        for chunk in chunk_message(text):
            await channel.send(chunk)

    async def send_attachments(self, attachments: list[tuple[str, bytes]]) -> None:
        await send_attachments(await self._channel(), attachments)

    async def send_reminder(
        self, reminder_id: int, text: str, category: str | None, time_label: str,
        late_for: str | None, weather: str | None = None,
        ping_number: int = 1, ping_total: int | None = None,
    ) -> int:
        if ping_number > 1 and ping_total:
            card = reping_card(text, category, time_label, ping_number, ping_total)
        else:
            card = active_ping_card(text, category, time_label, late_for, weather)
        channel = await self._channel()
        message = await channel.send(
            f"<@{self._owner_id}>", embed=ping_embed(card), view=reminder_view(reminder_id)
        )
        return message.id

    async def send_nudge(self, lines: list[str]) -> int:
        description = "\n".join(lines)
        if len(description) > 4000:
            description = description[:3999] + "…"
        embed = discord.Embed(title=NUDGE_TITLE, description=description, color=LIST_EMBED_COLOR)
        channel = await self._channel()
        message = await channel.send(f"<@{self._owner_id}>", embed=embed)
        return message.id

    async def edit_ping(self, message_id: int, reminder_id: int, card: PingCard) -> None:
        channel = await self._channel()
        view = reminder_view(reminder_id) if card.buttons else None
        try:
            await channel.get_partial_message(message_id).edit(
                content=f"<@{self._owner_id}>", embed=ping_embed(card), view=view
            )
        except discord.NotFound as exc:
            raise PingMessageMissing(message_id) from exc

    async def delete_ping(self, message_id: int) -> None:
        try:
            channel = await self._channel()
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass  # already gone — nothing to clean up
        except discord.DiscordException as exc:
            logger.warning("failed to delete old ping %s: %s", message_id, exc)

    async def post_list(self, render: ListRender) -> int:
        channel = await self._channel()
        message = await channel.send(
            content=None, embed=list_embed(render), view=list_view(render.buttons)
        )
        try:
            await message.pin()
        except discord.DiscordException as exc:
            logger.warning("failed to pin the daily list: %s", exc)
        return message.id

    async def edit_list(self, message_id: int, render: ListRender) -> None:
        channel = await self._channel()
        try:
            await channel.get_partial_message(message_id).edit(
                content="", embed=list_embed(render), view=list_view(render.buttons)
            )
        except discord.NotFound as exc:
            raise ListMessageMissing(message_id) from exc

    async def unpin(self, message_id: int) -> None:
        try:
            channel = await self._channel()
            await channel.get_partial_message(message_id).unpin()
        except discord.DiscordException as exc:
            logger.warning("failed to unpin message %s: %s", message_id, exc)


async def run_bypass_tick(conn, sender, now: datetime, tz=None) -> None:
    """Posts the 15-minute bypass warning once, and "Bypass ended" once at expiry.
    The DB (not memory) records both, so restarts and both instances agree."""
    if await asyncio.to_thread(end_expired_bypass, conn, now):
        await sender.send_text("🛡️ Bypass ended")
        return
    until = await asyncio.to_thread(get_bypass_until, conn, now)
    if until is None or until - now > BYPASS_WARNING_LEAD:
        return
    if not await asyncio.to_thread(claim_bypass_warning, conn, now):
        return
    tz = tz or resolve_timezone()
    minutes = max(1, int((until - now).total_seconds() // 60))
    await sender.send_text(
        f"⏳ Bypass ends in {minutes} min (at {until.astimezone(tz):%H:%M}). "
        "Type bypass to renew it."
    )


def should_respond(
    channel_id: int,
    author_id: int,
    author_is_bot: bool,
    allowed_channel_id: int,
    owner_id: int,
) -> bool:
    if author_is_bot:
        return False
    if channel_id != allowed_channel_id:
        return False
    if author_id != owner_id:
        return False
    return True


def chunk_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    if not text:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


_SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/heic",
    "image/heif",
}

MAX_IMAGE_SIZE_BYTES = 7 * 1024 * 1024


def _clean_mime_type(content_type: str) -> str:
    return content_type.split(";")[0].strip().lower()


def _find_image_attachment(attachments):
    for attachment in attachments:
        if not attachment.content_type:
            continue
        if _clean_mime_type(attachment.content_type) in _SUPPORTED_IMAGE_MIME_TYPES:
            return attachment
    return None


async def _resolve_replied_to(message: discord.Message) -> str | None:
    reference = getattr(message, "reference", None)
    if reference is None:
        return None
    resolved = getattr(reference, "resolved", None)
    if resolved is None:
        try:
            resolved = await message.channel.fetch_message(reference.message_id)
        except Exception as exc:
            logger.warning("failed to resolve replied-to message: %s", exc)
            return None
    return getattr(resolved, "content", None) or None


def resolve_discord_config() -> tuple[str, int, int]:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN is not set")

    channel_id_raw = os.getenv("DISCORD_CHANNEL_ID")
    if not channel_id_raw:
        raise ValueError("DISCORD_CHANNEL_ID is not set")
    try:
        channel_id = int(channel_id_raw)
    except ValueError:
        raise ValueError(
            f"DISCORD_CHANNEL_ID must be a number, got: {channel_id_raw!r}"
        )

    owner_id_raw = os.getenv("DISCORD_OWNER_ID")
    if not owner_id_raw:
        raise ValueError("DISCORD_OWNER_ID is not set")
    try:
        owner_id = int(owner_id_raw)
    except ValueError:
        raise ValueError(
            f"DISCORD_OWNER_ID must be a number, got: {owner_id_raw!r}"
        )

    return token, channel_id, owner_id


class JarvisDiscordClient(discord.Client):
    def __init__(
        self,
        conn,
        gemini_client,
        allowed_channel_id,
        owner_id,
        database_url=None,
        instance_role="local",
        brains: Brains | None = None,
        **kwargs,
    ):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, **kwargs)
        self._conn = conn
        self._gemini_client = gemini_client
        self._allowed_channel_id = allowed_channel_id
        self._owner_id = owner_id
        self._database_url = database_url
        self._instance_role = instance_role
        self._brains = brains
        self._lock = asyncio.Lock()
        self._heartbeat_task = None
        self._heartbeat_conn = (
            init_db(database_url) if instance_role == "local" and database_url else None
        )
        self._consolidation_task = None
        self._consolidation_conn = (
            init_db(database_url) if instance_role == "cloud" and database_url else None
        )
        self._scheduler_task = None
        self._scheduler_conn = (
            init_db(database_url) if instance_role == "cloud" and database_url else None
        )
        self._queue_conn = (
            init_db(database_url) if instance_role == "local" and database_url else None
        )
        self._queue_task = None
        self._last_prune_date: date | None = None
        self._discord_loop: asyncio.AbstractEventLoop | None = None
        self._computer_enabled = False
        self._tainted_turn = False  # set while a queued task runs (see _run_queued_task)
        self._carry_taint = False   # the previous chat turn was tainted -> taint the next one
        self._last_limit_hit: str | None = None
        self._last_task_stopped = False
        self._stale_cleared = False
        self._command_conn = None
        self._last_task_tainted = False
        # Open computer tasks of the current turn: task id -> ProgressLine
        # (which holds the ComputerTaskState the Stop button sets).
        self._tasks: dict[str, ProgressLine] = {}
        self.approvals = PendingApprovals()
        self.approval_broker = DiscordApprovalBroker(
            self, allowed_channel_id, owner_id, self.approvals
        )
        if database_url:
            # Computer access needs the shared DB (bypass state, queue, log).
            self.enable_computer_mode()

    # ------------------------------------------------------------ computer mode

    def enable_computer_mode(self) -> bool:
        """Gives each turn a fresh computer-mode ClaudeBrain: machine "pc" on the
        local instance, "server" on the cloud one. Without a Claude brain nothing
        changes (A1 behaviour)."""
        base = self._brains.claude if self._brains is not None else None
        if not isinstance(base, ClaudeBrain):
            return False
        machine = "pc" if self._instance_role == "local" else "server"
        self._brains.computer_machine = machine
        self._brains.computer_claude_factory = lambda: self._new_computer_brain(base, machine)
        self._computer_enabled = True
        return True

    def _new_computer_brain(self, base: ClaudeBrain, machine: str) -> ClaudeBrain:
        # Runs in the run_turn worker thread. One ComputerTaskState per task,
        # registered so the Stop button on this task's progress line reaches it.
        # Queued text was written by the cloud model (possibly prompt-injected),
        # so a queued task starts tainted: bypass suspended, risky steps ask.
        # A chat turn that followed a tainted one ("summarize this page", then "do it")
        # also starts tainted; the flag lasts one turn (see _run_chat_turn).
        inherited = self._tainted_turn or self._carry_taint
        state = ComputerTaskState(task_id=uuid.uuid4().hex[:12], tainted=inherited)
        sender = DiscordSender(self, self._allowed_channel_id, self._owner_id)
        progress = ProgressLine(sender, state)
        self._tasks[state.task_id] = progress
        database_url = self._database_url
        config = ComputerConfig(
            machine=machine,
            os_name="windows" if sys.platform == "win32" else "linux",
            home=os.path.expanduser("~"),
            jarvis_dir=os.path.dirname(os.path.abspath(__file__)),
            broker=self.approval_broker,
            conn_factory=lambda: _connect(database_url),
            progress=progress.update,
            send_file=self._send_file,
            max_steps=resolve_computer_task_max_steps(),
            timeout_s=resolve_computer_task_timeout_minutes() * 60,
            discord_loop=self._discord_loop,
            queue_task=self._queue_for_pc if machine == "server" else None,
            state=state,
        )
        return ClaudeBrain(
            base.oauth_token, base.model, timeout=base.timeout,
            cli_path=base.cli_path, computer=config,
        )

    async def _send_file(self, path: str) -> str:
        # Called on the Discord loop, only after the guard/approval allowed it.
        try:
            size = await asyncio.to_thread(os.path.getsize, path)
            if size > MAX_SEND_FILE_BYTES:
                return f"ERROR: {os.path.basename(path)} is too large to send ({size // (1024 * 1024)} MB)"
            data = await asyncio.to_thread(lambda: open(path, "rb").read())
            channel = await DiscordSender(self, self._allowed_channel_id, self._owner_id)._channel()
            await channel.send(files=[discord.File(io.BytesIO(data), filename=os.path.basename(path))])
        except Exception as exc:
            return f"ERROR: couldn't send the file: {exc}"
        return f"Sent {os.path.basename(path)} to Discord."

    async def _queue_for_pc(self, text: str) -> str:
        def queue() -> int:
            conn = _connect(self._database_url)
            try:
                return queue_pc_task(conn, text, datetime.now(timezone.utc))
            finally:
                conn.close()

        task_id = await asyncio.to_thread(queue)
        return f"Queued as task #{task_id}. Your PC is off — I'll do this when it's back on."

    async def _finish_tasks(self) -> None:
        tasks, self._tasks = list(self._tasks.values()), {}
        self._last_limit_hit = next(
            (p.state.limit_hit for p in tasks if p.state.limit_hit), None
        )
        self._last_task_tainted = any(p.state.self_tainted for p in tasks)
        self._last_task_stopped = any(p.state.stopped for p in tasks)
        for progress in tasks:
            await progress.finish()

    async def _run_chat_turn(self, text: str, **kwargs) -> tuple[str, list]:
        """run_turn in a worker thread, with computer tasks wired to this loop."""
        self._discord_loop = asyncio.get_running_loop()
        attachments: list[tuple[str, bytes]] = []
        outcome = kwargs.pop("outcome_out", None)
        if outcome is None:
            outcome = {}
        queued = self._tainted_turn
        try:
            reply = await asyncio.to_thread(
                run_turn, self._conn, self._gemini_client, text,
                brains=self._brains, attachments_out=attachments,
                outcome_out=outcome, **kwargs,
            )
        finally:
            await self._finish_tasks()
            if not queued:
                # Taint carries into exactly the next chat turn: this turn's own taint
                # (its task read untrusted content itself, or a Jarvis web info tool
                # ran) counts; a merely inherited taint does not chain further.
                own = self._last_task_tainted
                web = any(t in _WEB_TAINT_TOOLS for t in outcome.get("tools", []))
                self._carry_taint = own or web
        return reply, attachments

    async def _process_pc_queue(self) -> None:
        """One pass over the PC queue. The first pass also fails runs a crash left
        'running'. Every pass drops week-old tasks with a note, then runs the queued
        ones one at a time, each claimed (atomically) while holding the chat lock."""
        sender = DiscordSender(self, self._allowed_channel_id, self._owner_id)
        conn = self._queue_conn = await asyncio.to_thread(
            ensure_connected, self._queue_conn, self._database_url
        )
        now = datetime.now(timezone.utc)
        if not self._stale_cleared:
            await asyncio.to_thread(fail_stale_pc_tasks, conn, now)
            self._stale_cleared = True
        for task in await asyncio.to_thread(drop_expired_pc_tasks, conn, now):
            await sender.send_text(
                f"🗑️ Dropped queued PC task #{task['id']} (older than 7 days): "
                f"{task['text'][:200]}"
            )
        while True:
            if self._lock.locked():
                return  # a chat turn / task is running: try again on the next poll
            async with self._lock:
                task = await asyncio.to_thread(next_pc_task, conn, datetime.now(timezone.utc))
                if task is None:
                    return
                await self._run_queued_task(conn, sender, task)

    async def _run_queued_task(self, conn, sender, task: dict) -> None:
        await sender.send_text(f"▶ Starting queued task: {task['text'][:300]}")
        status, note, attachments = "done", "", []
        outcome: dict = {}
        try:
            if self._database_url:
                self._conn = await asyncio.to_thread(
                    ensure_connected, self._conn, self._database_url
                )
            # The caller holds self._lock. Queued text never runs chat commands, runs
            # on Claude only (no fallback brain), starts tainted and is stored in
            # history labelled as a queued task.
            self._tainted_turn = True
            try:
                reply, attachments = await self._run_chat_turn(
                    task["text"], allow_commands=False, tier_override="claude_only",
                    outcome_out=outcome, queued_task=True,
                )
            finally:
                self._tainted_turn = False
            if not (outcome.get("ok") and outcome.get("brain") == "sonnet"):
                status, note = "failed", (reply or "")[:200]
                reply = f"⚠️ Queued task failed: {reply}"
            elif self._last_task_stopped:
                status, note = "cancelled", "stopped by the owner"
            elif self._last_limit_hit:
                status, note = "partial", f"stopped at the {self._last_limit_hit} limit"
        except Exception as exc:
            logger.warning("queued task %s failed: %s", task["id"], exc)
            status, note = "failed", str(exc)[:200]
            reply = f"⚠️ Queued task failed: {exc}"
        try:
            await asyncio.to_thread(finish_pc_task, conn, task["id"], status, note)
        except Exception as exc:
            logger.warning("couldn't record the end of queued task %s: %s", task["id"], exc)
        try:
            await sender.send_text(reply)
            if attachments:
                await sender.send_attachments(attachments)
        except Exception as exc:
            logger.warning("couldn't post the result of queued task %s: %s", task["id"], exc)

    async def _prune_activity_if_due(self, now: datetime) -> None:
        if self._last_prune_date == now.date():
            return
        await asyncio.to_thread(prune_activity, self._scheduler_conn, now)
        self._last_prune_date = now.date()

    async def setup_hook(self) -> None:
        self.add_dynamic_items(JarvisButton)

    async def on_ready(self) -> None:
        if self._instance_role == "local" and (
            self._heartbeat_task is None or self._heartbeat_task.done()
        ):
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        if (
            self._instance_role == "local" and self._computer_enabled
            and self._database_url and self._queue_task is None
        ):
            # Once per process: on a reconnect a task may genuinely be running.
            self._queue_task = asyncio.create_task(self._pc_queue_runner())
        if self._instance_role == "cloud" and (
            self._consolidation_task is None or self._consolidation_task.done()
        ):
            self._consolidation_task = asyncio.create_task(self._consolidation_loop())
        if self._instance_role == "cloud" and (
            self._scheduler_task is None or self._scheduler_task.done()
        ):
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def _pc_queue_runner(self) -> None:
        # Startup pass, then a poll so tasks the cloud queues later are picked up too.
        while True:
            try:
                await self._process_pc_queue()
            except Exception as exc:
                logger.warning("PC task queue pickup failed: %s", exc)
            await asyncio.sleep(QUEUE_POLL_SECONDS)

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                if self._database_url:
                    self._heartbeat_conn = await asyncio.to_thread(
                        ensure_connected, self._heartbeat_conn, self._database_url
                    )
                await asyncio.to_thread(touch_local_heartbeat, self._heartbeat_conn)
            except Exception as exc:
                logger.warning("failed to update local heartbeat: %s", exc)
            await asyncio.sleep(12)

    async def _consolidation_loop(self) -> None:
        while True:
            try:
                if self._database_url:
                    self._consolidation_conn = await asyncio.to_thread(
                        ensure_connected, self._consolidation_conn, self._database_url
                    )
                created = await asyncio.to_thread(
                    consolidate_old_messages, self._consolidation_conn, self._gemini_client
                )
                if created:
                    logger.info("consolidation created %d note(s)", created)
            except Exception as exc:
                logger.warning("consolidation loop iteration failed: %s", exc)
            await asyncio.sleep(CONSOLIDATION_INTERVAL_SECONDS)

    async def _scheduler_loop(self) -> None:
        sender = DiscordSender(self, self._allowed_channel_id, self._owner_id)
        while True:
            try:
                if self._database_url:
                    self._scheduler_conn = await asyncio.to_thread(
                        ensure_connected, self._scheduler_conn, self._database_url
                    )
                await run_scheduler_tick(
                    self._scheduler_conn, sender, datetime.now(timezone.utc),
                    resolve_timezone(), resolve_todo_list_time(), resolve_todo_list_lead_minutes(),
                    resolve_nudge_time(),
                )
            except Exception as exc:
                logger.warning("scheduler tick failed: %s", exc)
            try:
                now = datetime.now(timezone.utc)
                await run_bypass_tick(self._scheduler_conn, sender, now)
                await self._prune_activity_if_due(now)
            except Exception as exc:
                logger.warning("bypass/activity tick failed: %s", exc)
            await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)

    async def _handle_approval_button(self, interaction: discord.Interaction, custom_id: str) -> None:
        # Exempt from local-wins: an approval id lives only in the memory of the
        # instance that created it, and any other instance stays silent.
        _, approval_id, action = custom_id.split(":")
        if not self.approvals.knows(approval_id):
            return
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "This request isn't yours to answer.", ephemeral=True
            )
            return
        allowed = action == "allow"
        approver = getattr(interaction.user, "display_name", None) or str(interaction.user.id)
        if not self.approvals.resolve(approval_id, allowed, approver):
            await interaction.response.send_message("This request already ended.", ephemeral=True)
            return
        title = f"✅ Allowed by {approver}" if allowed else "🚫 Denied"
        try:
            await interaction.response.defer()
            await interaction.edit_original_response(
                embed=approval_outcome_embed(title), view=None
            )
        except Exception as exc:  # the decision already stands
            logger.warning("couldn't update the approval card: %s", exc)

    async def _handle_stop_button(self, interaction: discord.Interaction, custom_id: str) -> None:
        # Like approvals, exempt from local-wins: only the instance running the
        # task knows it; any other instance stays silent.
        progress = self._tasks.get(custom_id.split(":", 1)[1])
        if progress is None:
            return  # unknown (finished, restarted, or another instance's task): stay silent
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "This task isn't yours to stop.", ephemeral=True
            )
            return
        progress.state.stop()
        try:
            await interaction.response.defer()
            await interaction.edit_original_response(
                content="🛑 Stopping after the current step…", view=None
            )
        except Exception as exc:  # the stop already stands
            logger.warning("couldn't update the progress line: %s", exc)

    async def handle_button(self, interaction: discord.Interaction, custom_id: str) -> None:
        if custom_id.startswith("appr:"):
            await self._handle_approval_button(interaction, custom_id)
            return
        if custom_id.startswith("stop:"):
            await self._handle_stop_button(interaction, custom_id)
            return
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("These buttons aren't for you.", ephemeral=True)
            return
        try:
            if self._database_url:
                self._conn = await asyncio.to_thread(
                    ensure_connected, self._conn, self._database_url
                )
            if self._instance_role == "cloud":
                if await asyncio.to_thread(is_local_heartbeat_fresh, self._conn):
                    return  # the local instance answers this click
        except Exception as exc:
            logger.warning("button %s: memory unreachable: %s", custom_id, exc)
            try:
                await interaction.response.send_message(
                    "Couldn't reach memory — try again.", ephemeral=True
                )
            except discord.DiscordException:
                pass  # the other instance may have already acked this click
            return
        await interaction.response.defer()
        try:
            async with self._lock:
                outcome = await asyncio.to_thread(
                    apply_button, self._conn, custom_id,
                    datetime.now(timezone.utc), resolve_timezone(), interaction.message.id,
                )
        except ItemNotFoundError:
            await interaction.followup.send("That item no longer exists.", ephemeral=True)
            return
        except Exception as exc:
            logger.warning("button %s failed: %s", custom_id, exc)
            await interaction.followup.send("Couldn't reach memory — try again.", ephemeral=True)
            return

        if outcome.kind == "list" and interaction.message.id != outcome.list_message_id:
            # A click on an old list: the tick is recorded and today's pinned
            # list refreshes via the dirty flag; don't overwrite the old one.
            await interaction.followup.send("Ticked — the pinned list shows the latest.", ephemeral=True)
            return
        if outcome.delete_message:
            try:
                await interaction.delete_original_response()
                return
            except discord.DiscordException as exc:
                logger.warning("couldn't delete the done ping, showing the done card: %s", exc)
        if outcome.kind == "list":
            edit_kwargs = {
                "content": "",
                "embed": list_embed(outcome.list_render),
                "view": list_view(outcome.list_render.buttons),
            }
        else:
            reminder_id = int(custom_id.rsplit(":", 1)[-1])
            edit_kwargs = {
                "content": f"<@{self._owner_id}>",
                "embed": ping_embed(outcome.card),
                "view": reminder_view(reminder_id) if outcome.card.buttons else None,
            }
        try:
            await interaction.edit_original_response(**edit_kwargs)
        except discord.DiscordException as exc:
            logger.warning("couldn't update message after button %s: %s", custom_id, exc)
            try:
                await interaction.followup.send(
                    "Done — but I couldn't update that message.", ephemeral=True
                )
            except discord.DiscordException:
                pass

    def _command_connection(self):
        conn = self._command_conn
        if conn is None:
            return _connect(self._database_url)
        return ensure_connected(conn, self._database_url)

    async def _handle_command_message(self, message: discord.Message) -> bool:
        """True when the message was a command and has been answered (or deliberately
        left to the local instance)."""
        try:
            conn = self._command_conn = await asyncio.to_thread(self._command_connection)
            if self._instance_role == "cloud" and await asyncio.to_thread(
                is_local_heartbeat_fresh, conn
            ):
                return True  # the local instance answers
            handled = await asyncio.to_thread(handle_command, conn, message.content)
            if handled is None:
                return False
            reply, files = handled
            try:  # same history as a normal turn; plain inserts are safe without the lock
                await asyncio.to_thread(add_message, conn, "user", message.content)
                await asyncio.to_thread(add_message, conn, "assistant", reply)
            except Exception as exc:
                logger.warning("couldn't store a chat command in history: %s", exc)
        except Exception as exc:
            logger.warning("chat command failed: %s", exc)
            reply, files = "I can't reach my storage right now, try again in a moment.", []
        for chunk in chunk_message(reply):
            try:
                await message.channel.send(chunk)
            except discord.DiscordException as exc:
                logger.warning("failed to send Discord message: %s", exc)
        if files:
            await send_attachments(message.channel, files)
        return True

    async def on_message(self, message: discord.Message) -> None:
        if not should_respond(
            channel_id=message.channel.id,
            author_id=message.author.id,
            author_is_bot=message.author.bot,
            allowed_channel_id=self._allowed_channel_id,
            owner_id=self._owner_id,
        ):
            return

        if self._database_url and is_command_word(message.content):
            # Commands only touch the DB: answer them now, on their own connection,
            # instead of queueing behind a running task (so /safe works mid-task).
            if await self._handle_command_message(message):
                return

        if self._database_url:
            self._conn = await asyncio.to_thread(
                ensure_connected, self._conn, self._database_url
            )

        if self._instance_role == "cloud":
            is_fresh = await asyncio.to_thread(is_local_heartbeat_fresh, self._conn)
            if is_fresh:
                return

        image_attachment = _find_image_attachment(message.attachments)
        if image_attachment is not None and image_attachment.size > MAX_IMAGE_SIZE_BYTES:
            await message.channel.send(
                "That image is too large "
                f"({image_attachment.size // (1024 * 1024)} MB) — please send "
                f"one under {MAX_IMAGE_SIZE_BYTES // (1024 * 1024)} MB."
            )
            return

        try:
            image_bytes = None
            image_mime_type = None
            if image_attachment is not None:
                image_bytes = await image_attachment.read()
                image_mime_type = _clean_mime_type(image_attachment.content_type)

            replied_to = await _resolve_replied_to(message)

            async with message.channel.typing():
                async with self._lock:
                    reply, attachments = await self._run_chat_turn(
                        message.content,
                        image_bytes=image_bytes,
                        image_mime_type=image_mime_type,
                        replied_to=replied_to,
                    )
        except Exception as exc:
            logger.warning("failed to process message: %s", exc)
            reply, attachments = "Sorry, I couldn't process that — try again.", []

        for chunk in chunk_message(reply):
            try:
                await message.channel.send(chunk)
            except discord.DiscordException as exc:
                logger.warning("failed to send Discord message: %s", exc)
        if attachments:
            await send_attachments(message.channel, attachments)


def main() -> None:
    api_key = resolve_api_key()
    if not api_key:
        print(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add your "
            "free Gemini API key from https://aistudio.google.com/apikey"
        )
        sys.exit(1)

    try:
        token, channel_id, owner_id = resolve_discord_config()
    except ValueError as exc:
        print(f"Discord configuration problem: {exc}. Check your .env file.")
        sys.exit(1)

    try:
        models = resolve_models()
    except ValueError as exc:
        print(f"Configuration problem: {exc}. Check your .env file.")
        sys.exit(1)

    database_url = resolve_database_url()
    conn = init_db(database_url)
    gemini_client = GeminiClient(api_key=api_key, models=models)
    instance_role = resolve_instance_role()

    groq_api_key = resolve_groq_api_key()
    groq_model = resolve_groq_model()
    brains = Brains(
        gemini=GeminiBrain(gemini_client),
        claude=ClaudeBrain(
            resolve_claude_oauth_token(),
            resolve_claude_model(),
            cli_path=resolve_claude_cli_path(),
        ),
        groq=GroqBrain(groq_api_key, groq_model) if groq_api_key else None,
        groq_api_key=groq_api_key,
        groq_model=groq_model,
        show_tag=resolve_show_brain(),
    )

    client = JarvisDiscordClient(
        conn=conn,
        gemini_client=gemini_client,
        allowed_channel_id=channel_id,
        owner_id=owner_id,
        database_url=database_url,
        instance_role=instance_role,
        brains=brains,
    )
    client.run(token)


if __name__ == "__main__":
    main()
