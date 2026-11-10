import json
import logging
import re
import time
from dataclasses import dataclass

from brain.brains.groq import GroqUnavailable, groq_chat

logger = logging.getLogger(__name__)

TIERS = ("small_talk", "normal", "hard")

_TIER_ORDER = {tier: index for index, tier in enumerate(TIERS)}

# Explicit override phrases, checked before the judge ever runs. Order
# matters only as a tie-breaker when two phrases start at the same
# position, which can't happen in practice since they're distinct words.
_OVERRIDE_PHRASES = (
    ("use claude", "hard"),
    ("ask claude", "hard"),
    ("think hard", "hard"),
    ("think carefully", "hard"),
    ("use gemini", "normal"),
)

# Leftover punctuation/whitespace right where the override phrase used to
# be, e.g. "use claude: do X" -> ": do X" -> "do X".
_LEADING_STRIP_RE = re.compile(r"^[\s:;,.\-–—!?]+")

_JUDGE_TIMEOUT = 4.0

# Judge failure handling: log each failure reason at most once per window,
# and after a rate limit stop paying for a call that will just fail again.
_LOG_WINDOW_SECONDS = 600.0
_RATE_LIMIT_COOLDOWN_SECONDS = 600.0
_last_warned: dict[str, float] = {}
_judge_paused_until = 0.0


def _warn_judge_failure(reason: str, detail: str) -> None:
    now = time.monotonic()
    last = _last_warned.get(reason)
    if last is not None and now - last < _LOG_WINDOW_SECONDS:
        return
    _last_warned[reason] = now
    logger.warning("Groq judge failed (%s): %s — routing as normal", reason, detail)

# Kept small on purpose: the judge call must stay cheap (target <800
# tokens/call on Groq's free tier) so a busy conversation never blows the
# daily request/token budget just to classify a message. The message cap
# (1500) is the plan's value — high enough that a real, complex message
# almost never actually gets cut (worst case here still leaves the total
# call well under the 4000-char/test budget); when it IS cut, a visible
# marker is appended so the judge's own "if in doubt, choose the higher
# tier" rule has something to notice and act on.
_JUDGE_HISTORY_MESSAGES = 4
_JUDGE_HISTORY_ENTRY_CHARS = 150
_JUDGE_MESSAGE_CHARS = 1500
_TRUNCATION_MARKER = " …[message truncated — it is longer than shown]"

JUDGE_PROMPT = """You are Jarvis's routing judge. Read the new user message (with a little \
recent context) and classify it into exactly one tier:

- small_talk: greetings, thanks, acknowledgements, goodbyes, pure pleasantries — nothing \
that asks for information, a decision, an action, or memory. Answer it yourself in "reply". \
A message that mixes pleasantries with any request, question or task (e.g. "thanks, and \
remind me at 5") is NOT small_talk.
- normal: anything involving tools (reminders, to-do list, notes/memory, web, weather, opening \
a saved app or folder), everyday questions, short explanations, casual chat with real substance.
- hard: planning, analysis, comparisons/decisions with trade-offs, long or careful writing, \
coding, debugging, multi-step reasoning, or anything the user frames as important or complex.
- hard also: anything about the computer itself (files, folders, documents, programs, shell commands, installs, the server, logs, disk space, "on my PC") - even if it sounds simple.

A short answer that responds to or confirms something Jarvis just asked or offered (e.g. 'yes', 'sure', 'do it', 'done') is normal, not small_talk.

Tiers rank small_talk < normal < hard. If you are unsure which tier fits, choose the higher \
one — never guess low.

When tier is small_talk, write "reply" the way Jarvis — a helpful, concise personal \
assistant who addresses the user as "sir" — would: one short, warm sentence, well under 200 \
characters. You cannot save, note, remember, set, add or schedule anything, so never claim \
to have done so (no "noted", "saved", "I'll remember that").

Respond with ONLY a JSON object and nothing else: \
{"tier": "small_talk" | "normal" | "hard", "reply": "<only when tier is small_talk>"}"""


_ACTION_CLAIM_RE = re.compile(
    r"\b(noted|saved|remember(ed)?|added|scheduled|reminder|set (a|an|the|for|to)|"
    r"logged|recorded|stored|booked|created)\b",
    re.IGNORECASE,
)


@dataclass
class RouteDecision:
    tier: str                    # one of TIERS
    reason: str                  # "override", "image", "judge", "no_judge", "judge_failed", "pending", "reply"
    cleaned_message: str         # message with override phrase removed (original if that leaves it empty)
    small_talk_reply: str | None = None


def _match_override(message: str) -> tuple[str, str] | None:
    best_match = None
    best_tier = None
    for phrase, tier in _OVERRIDE_PHRASES:
        m = re.search(r"\b" + re.escape(phrase) + r"\b", message, re.IGNORECASE)
        if m and (best_match is None or m.start() < best_match.start()):
            best_match = m
            best_tier = tier

    if best_match is None:
        return None

    rest = message[: best_match.start()] + message[best_match.end() :]
    collapsed = re.sub(r"\s+", " ", rest)
    cleaned = _LEADING_STRIP_RE.sub("", collapsed).strip()
    return best_tier, (cleaned or message)


def _build_judge_messages(message: str, history: list[dict]) -> list[dict]:
    recent = history[-_JUDGE_HISTORY_MESSAGES:]
    lines = [
        f"{h.get('role', 'user')}: {str(h.get('content', ''))[:_JUDGE_HISTORY_ENTRY_CHARS]}"
        for h in recent
    ]
    history_text = "\n".join(lines) if lines else "(no prior messages)"
    if len(message) > _JUDGE_MESSAGE_CHARS:
        trimmed_message = message[:_JUDGE_MESSAGE_CHARS] + _TRUNCATION_MARKER
    else:
        trimmed_message = message
    user_content = (
        f"Recent conversation:\n{history_text}\n\nNew message:\n{trimmed_message}"
    )
    return [
        {"role": "system", "content": JUDGE_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _ask_judge(
    message: str, history: list[dict], groq_api_key: str, groq_model: str
) -> tuple[str, str, str | None]:
    global _judge_paused_until
    if time.monotonic() < _judge_paused_until:
        return "normal", "judge_failed", None

    messages = _build_judge_messages(message, history)
    try:
        raw = groq_chat(
            groq_api_key, groq_model, messages, json_mode=True, timeout=_JUDGE_TIMEOUT,
            reasoning_effort="low",
        )
        parsed = json.loads(raw)
    except GroqUnavailable as exc:
        kind = getattr(exc, "kind", "other")
        if kind == "rate_limit":
            _judge_paused_until = time.monotonic() + _RATE_LIMIT_COOLDOWN_SECONDS
        _warn_judge_failure(kind, str(exc))
        return "normal", "judge_failed", None
    except (json.JSONDecodeError, TypeError) as exc:
        _warn_judge_failure("bad_json", str(exc))
        return "normal", "judge_failed", None

    if not isinstance(parsed, dict):
        return "normal", "judge_failed", None

    tier = parsed.get("tier")
    if tier not in TIERS:
        return "normal", "judge_failed", None

    if tier == "small_talk":
        reply = parsed.get("reply")
        if not isinstance(reply, str) or not reply or len(reply) > 400:
            return "normal", "judge_failed", None
        if _ACTION_CLAIM_RE.search(reply):
            # Groq has no tools, so a reply claiming it saved/noted/set
            # something would be false — let a brain that can act answer.
            return "normal", "judge_failed", None
        return "small_talk", "judge", reply

    return tier, "judge", None


def _apply_floor(tier: str, reason: str, condition: bool, floor_reason: str) -> tuple[str, str]:
    if condition and _TIER_ORDER[tier] < _TIER_ORDER["normal"]:
        return "normal", floor_reason
    return tier, reason


def route(
    message: str,
    has_image: bool,
    history: list[dict],
    has_pending: bool,
    groq_api_key: str | None,
    groq_model: str,
    is_reply: bool = False,
    continues_claude: bool = False,
) -> RouteDecision:
    override = _match_override(message)
    if override is not None:
        tier, cleaned_message = override
        reason = "override"
        small_talk_reply = None
    else:
        cleaned_message = message
        if not groq_api_key:
            tier, reason, small_talk_reply = "normal", "no_judge", None
        else:
            tier, reason, small_talk_reply = _ask_judge(
                cleaned_message, history, groq_api_key, groq_model
            )

    tier, reason = _apply_floor(tier, reason, has_image, "image")
    tier, reason = _apply_floor(tier, reason, has_pending, "pending")
    # A reply to one of Jarvis's own messages arrives without the
    # "[Replying to: ...]" context the judge would need, so a bare "yes"
    # would look like small talk.
    tier, reason = _apply_floor(tier, reason, is_reply, "reply")
    # Claude answered moments ago: a follow-up ("yes", "now put it on the right") belongs to
    # the conversation Claude is in the middle of — Gemini would lose that thread. Pure
    # small talk and an explicit "use gemini" still go where they were sent.
    if continues_claude and reason != "override" and tier == "normal":
        tier, reason = "hard", "continues"
    if tier != "small_talk":
        small_talk_reply = None

    return RouteDecision(
        tier=tier, reason=reason, cleaned_message=cleaned_message, small_talk_reply=small_talk_reply
    )
