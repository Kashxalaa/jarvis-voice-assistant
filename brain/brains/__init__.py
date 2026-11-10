from dataclasses import dataclass, field
from typing import Protocol

FAIL_UNAVAILABLE = "unavailable"  # brain down / error / timeout / empty
FAIL_LIMIT = "limit"              # subscription/quota limit
FAIL_CONFIG = "config"            # misconfiguration (bad key, model not found)


@dataclass
class TurnContext:
    conn: object
    system_prompt: str
    profile: str                  # the owner's About-me document ("" when none)
    history: list[dict]
    message: str                  # stored_message (may include the [Replying to: …] prefix)
    image_bytes: bytes | None
    image_mime_type: str | None
    tools: list[dict]             # Jarvis tool declarations for this turn
    executor: "ToolExecutor"      # noqa: F821 - brain.tools.ToolExecutor, kept as a string to avoid a cycle


@dataclass
class BrainResult:
    text: str | None              # final answer (None when failed)
    brain: str                    # "gemini" | "sonnet" | "groq"
    tools_ran: list[str] = field(default_factory=list)  # names of tools executed, in order
    failure: str | None = None    # FAIL_* when failed
    failure_detail: str = ""      # e.g. reset time "15:45" for FAIL_LIMIT, or error text for logs
    handled_as_note: bool = False
    # Subset of tools_ran that changed something (anything not a pure
    # information lookup) — only these block falling back to another brain.
    side_effect_tools_ran: list[str] = field(default_factory=list)
    # Files to attach to the Discord reply, as (filename, bytes) - e.g. output.txt, changes.diff.
    attachments: list[tuple[str, bytes]] = field(default_factory=list)


class Brain(Protocol):
    name: str

    def respond(self, ctx: TurnContext) -> BrainResult: ...
