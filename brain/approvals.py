import asyncio
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

APPROVAL_TIMEOUT_SECONDS = 600
_REMEMBERED_IDS = 1000


@dataclass(frozen=True)
class ApprovalRequest:
    machine: str               # "pc" | "server"
    summary: str               # from GuardDecision.summary
    detail: str                # exact command / file list (may be long)
    reason: str                # Claude's one-line reason if available, else ""
    batch_size: int = 1


@dataclass(frozen=True)
class ApprovalResult:
    allowed: bool
    outcome: str               # "allowed" | "denied" | "timeout" | "error"
    approver: str | None


class ApprovalBroker(Protocol):
    async def request(
        self, req: ApprovalRequest, timeout_s: float = APPROVAL_TIMEOUT_SECONDS
    ) -> ApprovalResult: ...


class PendingApprovals:
    """In-memory registry of open approvals. An approval id lives only in the
    instance that created it, so only that instance can resolve it."""

    def __init__(self) -> None:
        self._open: dict[str, asyncio.Future] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def create(self) -> tuple[str, asyncio.Future]:
        approval_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        with self._lock:
            self._open[approval_id] = future
            self._seen[approval_id] = None
            while len(self._seen) > _REMEMBERED_IDS:
                self._seen.popitem(last=False)
        return approval_id, future

    def knows(self, approval_id: str) -> bool:
        """True if this instance ever created the id (open or already ended)."""
        with self._lock:
            return approval_id in self._seen

    def resolve(self, approval_id: str, allowed: bool, approver: str) -> bool:
        """First caller wins. Safe from any thread or event loop."""
        with self._lock:
            future = self._open.pop(approval_id, None)
        if future is None or future.done():
            return False
        result = (allowed, approver)
        loop = future.get_loop()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            future.set_result(result)
        else:
            loop.call_soon_threadsafe(_set_if_pending, future, result)
        return True

    def discard(self, approval_id: str) -> None:
        with self._lock:
            future = self._open.pop(approval_id, None)
        if future is None or future.done():
            return
        loop = future.get_loop()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            future.cancel()
        else:
            loop.call_soon_threadsafe(future.cancel)


def _set_if_pending(future: asyncio.Future, result) -> None:
    if not future.done():
        future.set_result(result)
