import datetime
import json
import logging
import time
from zoneinfo import ZoneInfo

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

_NON_RETRYABLE_CODES = {400, 401, 403, 404}
_QUOTA_EXHAUSTED_CODES = {429}
_MODEL_NOT_FOUND_CODE = 404
_TRANSIENT_COOLDOWN_SECONDS = 180.0
_EMBEDDING_MODEL = "gemini-embedding-001"
_EMBEDDING_DIMENSIONS = 768
_QUOTA_RESET_TZ = ZoneInfo("America/Los_Angeles")


def _seconds_until_quota_reset(now: float) -> float:
    # Gemini's daily quotas reset at midnight Pacific time, not UTC.
    dt = datetime.datetime.fromtimestamp(now, tz=_QUOTA_RESET_TZ)
    next_midnight = datetime.datetime.combine(
        dt.date() + datetime.timedelta(days=1), datetime.time(), tzinfo=_QUOTA_RESET_TZ
    )
    return next_midnight.timestamp() - now


class GeminiUnavailableError(Exception):
    def __init__(self, message, code: int | None = None):
        super().__init__(message)
        self.code = code


class GeminiConfigError(Exception):
    def __init__(self, message, code: int | None = None):
        super().__init__(message)
        self.code = code


def _call_with_retry(fn, retries: int = 1, backoff_seconds: float = 2.0):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except genai_errors.APIError as exc:
            if exc.code in _NON_RETRYABLE_CODES:
                raise GeminiConfigError(exc.message or str(exc), code=exc.code) from exc
            if exc.code in _QUOTA_EXHAUSTED_CODES:
                # A daily quota won't refill within a short backoff, so
                # retrying the same model is pointless — fail immediately
                # so a caller cascading across models can move on fast.
                raise GeminiUnavailableError(exc.message or str(exc), code=exc.code) from exc
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff_seconds)
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff_seconds)
    raise GeminiUnavailableError(
        str(last_exc), code=getattr(last_exc, "code", None)
    ) from last_exc


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _build_conversation_prompt(system_prompt, profile, history, user_message) -> str:
    profile_text = (profile or "").strip() or "(no profile yet)"
    history_text = (
        "\n".join(f"{m['role']}: {m['content']}" for m in history)
        or "(no prior messages)"
    )
    return (
        f"{system_prompt}\n\n"
        f"About the owner (their profile):\n{profile_text}\n\n"
        f"Conversation so far:\n{history_text}\n\n"
        f"user: {user_message}\nassistant:"
    )


def _build_tool_result_prompt(
    system_prompt, profile, history, user_message, tool_name, tool_result
) -> str:
    conversation = _build_conversation_prompt(system_prompt, profile, history, user_message)
    # Strip the trailing "assistant:" cue so the tool output sits between
    # the user's message and the reply the model is about to write.
    conversation = conversation.removesuffix("assistant:").rstrip()
    return (
        f"{conversation}\n\n"
        f"[You called the tool {tool_name}. It returned:]\n{tool_result}\n\n"
        "Now answer the user's message directly, in your own words, using "
        "that result where it helps. Don't paste the raw output back. If the "
        "result is empty or doesn't answer the question, answer from your own "
        "knowledge and reasoning instead — don't tell the user you found "
        "nothing unless they explicitly asked what you have saved.\n"
        "assistant:"
    )


def _build_memory_review_prompt(profile: str, lessons: list[str], exchanges) -> str:
    history_text = "\n".join(f"{m['role']}: {m['content']}" for m in exchanges)
    lessons_text = "\n".join(f"{i}. {t}" for i, t in enumerate(lessons, start=1)) or "(none yet)"
    return (
        "You maintain the owner's About-me profile and the assistant's lessons, both below. Read the "
        "owner's recent messages.\n"
        "1. Profile: did the owner state anything LASTING about themselves that is missing or now "
        "wrong (goals, routines, projects, preferences, people, setup)? Ignore one-off moods/events. "
        'Each edit is {"section": <heading as written>, "action": "add"|"replace"|"remove", '
        '"old": <words from the existing line, for replace/remove>, "new": <full new line, for '
        "add/replace>}.\n"
        "2. Lessons: did the owner correct how the assistant behaved, or say how it should act in "
        "future, in a way no existing lesson covers? Express each as one short imperative rule about "
        "the assistant — never a fact about the owner.\n"
        "Never invent. Respond with ONLY a JSON object "
        '{"profile": [edits], "lessons": ["rule", ...]}; use empty lists when nothing should change.\n\n'
        f"Profile:\n{profile or '(empty)'}\n\nLessons:\n{lessons_text}\n\n"
        f"Owner's recent messages:\n{history_text}"
    )


def _build_fact_extraction_prompt(exchanges) -> str:
    history_text = "\n".join(f"{m['role']}: {m['content']}" for m in exchanges)
    return (
        "Read the conversation below. Extract any new, durable facts about "
        "the user (preferences, routines, names, identifiers). Respond with "
        "ONLY a JSON object mapping short snake_case keys to string values. "
        "If nothing fact-worthy was said, respond with {}.\n\n"
        f"{history_text}"
    )


def _build_note_summarization_prompt(exchanges) -> str:
    history_text = "\n".join(f"{m['role']}: {m['content']}" for m in exchanges)
    return (
        "Read the conversation excerpt below, from earlier in an ongoing "
        "conversation that has since moved on. Identify anything worth "
        "remembering long-term that isn't just a single simple fact — a "
        "decision, an explanation, or context that would help later. "
        "Respond with ONLY a JSON array of objects, each with 'title' "
        "(short), 'content' (1-3 sentences), 'tags' (comma-separated "
        "keywords), and 'importance' (integer 1-10). If nothing in this "
        "excerpt is worth keeping as a note, respond with [].\n\n"
        f"{history_text}"
    )


class ToolReply:
    def __init__(self, text: str | None, function_name: str | None, function_args: dict | None):
        self.text = text
        self.function_name = function_name
        self.function_args = function_args
        # Every function call in the reply, in order (Gemini can ask for several
        # at once, e.g. "add 2 reminders"); function_name/args are the first.
        self.calls: list[tuple[str, dict]] = (
            [(function_name, function_args or {})] if function_name else []
        )


class GeminiClient:
    def __init__(
        self, api_key: str, models: list[str] | None = None, client=None, now_fn=time.time
    ):
        self._client = client or genai.Client(api_key=api_key)
        self.models = models if models is not None else ["gemini-3.6-flash"]
        self._now = now_fn
        self._unavailable_until: dict[str, float] = {}

    def _call_across_models(self, attempt_fn):
        # With more than one model to fall back to, retrying the same
        # (already-failing) model before moving on just adds latency for
        # nothing — the next model is a strictly better use of that time.
        retries = 0 if len(self.models) > 1 else 1

        last_exc = None
        for model in self.models:
            if self._unavailable_until.get(model, 0) > self._now():
                continue
            try:
                return _call_with_retry(
                    lambda model=model: attempt_fn(model), retries=retries
                )
            except GeminiConfigError as exc:
                if exc.code != _MODEL_NOT_FOUND_CODE:
                    # Anything other than "model not found" (bad API key,
                    # permission denied, ...) is a global problem, not a
                    # per-model one — cascading would just hide it behind
                    # N pointless calls and an unhelpful generic message.
                    raise
                last_exc = exc
                self._unavailable_until[model] = (
                    self._now() + _seconds_until_quota_reset(self._now())
                )
                logger.warning("model %s not found, trying next: %s", model, exc)
            except GeminiUnavailableError as exc:
                last_exc = exc
                if exc.code in _QUOTA_EXHAUSTED_CODES:
                    cooldown = _seconds_until_quota_reset(self._now())
                else:
                    cooldown = _TRANSIENT_COOLDOWN_SECONDS
                self._unavailable_until[model] = self._now() + cooldown
                logger.warning("model %s unavailable, trying next: %s", model, exc)
        raise GeminiUnavailableError(
            f"all models exhausted: {', '.join(self.models)}"
        ) from last_exc

    def _call_generate(self, contents) -> str:
        def _do(model: str) -> str:
            response = self._client.models.generate_content(
                model=model, contents=contents
            )
            text = response.text
            if not text:
                raise ValueError("Gemini returned an empty response")
            return text

        return self._call_across_models(_do)

    def generate_reply(
        self,
        system_prompt,
        profile,
        history,
        user_message,
        image_bytes: bytes | None = None,
        image_mime_type: str | None = None,
    ) -> str:
        prompt = _build_conversation_prompt(system_prompt, profile, history, user_message)
        if image_bytes:
            contents = [
                prompt,
                genai_types.Part.from_bytes(data=image_bytes, mime_type=image_mime_type),
            ]
        else:
            contents = prompt
        return self._call_generate(contents)

    def generate_with_tools(
        self,
        system_prompt,
        profile,
        history,
        user_message,
        tools: list[dict],
        image_bytes: bytes | None = None,
        image_mime_type: str | None = None,
    ) -> ToolReply:
        prompt = _build_conversation_prompt(system_prompt, profile, history, user_message)
        if image_bytes:
            contents = [
                prompt,
                genai_types.Part.from_bytes(data=image_bytes, mime_type=image_mime_type),
            ]
        else:
            contents = prompt

        declarations = [genai_types.FunctionDeclaration(**tool) for tool in tools]
        config = genai_types.GenerateContentConfig(
            tools=[genai_types.Tool(function_declarations=declarations)]
        )

        def _do(model: str) -> ToolReply:
            response = self._client.models.generate_content(
                model=model, contents=contents, config=config
            )
            if not response.text and not response.function_calls:
                raise ValueError("Gemini returned an empty response")
            if response.function_calls:
                calls = [(fc.name, dict(fc.args or {})) for fc in response.function_calls]
                reply = ToolReply(
                    text=response.text, function_name=calls[0][0], function_args=calls[0][1]
                )
                reply.calls = calls
                return reply
            return ToolReply(text=response.text, function_name=None, function_args=None)

        return self._call_across_models(_do)

    def answer_with_tool_result(
        self,
        system_prompt,
        profile,
        history,
        user_message,
        tool_name: str,
        tool_result: str,
        image_bytes: bytes | None = None,
        image_mime_type: str | None = None,
    ) -> str:
        # A plain (tool-less) call, so the model has to answer now rather
        # than chaining into another tool call.
        prompt = _build_tool_result_prompt(
            system_prompt, profile, history, user_message, tool_name, tool_result
        )
        if image_bytes:
            contents = [
                prompt,
                genai_types.Part.from_bytes(data=image_bytes, mime_type=image_mime_type),
            ]
        else:
            contents = prompt
        return self._call_generate(contents)

    def review_memory(self, profile: str, lessons: list[str], exchanges) -> dict | None:
        """{"profile": [edits], "lessons": [rules]} (lists may be empty), or None when the
        attempt failed (retry later)."""
        try:
            raw = self._call_generate(_build_memory_review_prompt(profile, lessons, exchanges))
            parsed = json.loads(_strip_code_fences(raw))
        except (GeminiUnavailableError, GeminiConfigError, json.JSONDecodeError) as exc:
            logger.warning("memory review failed: %s", exc)
            return None
        if not isinstance(parsed, dict):
            logger.warning("memory review returned non-object JSON: %r", parsed)
            return None
        edits = parsed.get("profile")
        rules = parsed.get("lessons")
        return {
            "profile": [e for e in edits if isinstance(e, dict)] if isinstance(edits, list) else [],
            "lessons": [r for r in rules if isinstance(r, str) and r.strip()] if isinstance(rules, list) else [],
        }

    def extract_facts(self, exchanges) -> dict:
        prompt = _build_fact_extraction_prompt(exchanges)
        try:
            raw = self._call_generate(prompt)
        except (GeminiUnavailableError, GeminiConfigError) as exc:
            logger.warning("fact extraction call failed: %s", exc)
            return {}
        try:
            parsed = json.loads(_strip_code_fences(raw))
        except json.JSONDecodeError:
            logger.warning("fact extraction returned non-JSON output: %r", raw)
            return {}
        if not isinstance(parsed, dict):
            logger.warning("fact extraction returned non-object JSON: %r", parsed)
            return {}
        return {str(k): v for k, v in parsed.items() if isinstance(v, str)}

    def embed_text(self, text: str) -> list[float]:
        def _do() -> list[float]:
            response = self._client.models.embed_content(
                model=_EMBEDDING_MODEL,
                contents=text,
                config=genai_types.EmbedContentConfig(
                    output_dimensionality=_EMBEDDING_DIMENSIONS
                ),
            )
            if not response.embeddings:
                raise ValueError("Gemini returned no embeddings")
            return list(response.embeddings[0].values)

        return _call_with_retry(_do)

    def summarize_for_notes(self, exchanges) -> list[dict] | None:
        # None means "this attempt failed, worth retrying" (Gemini
        # unavailable or an unusable response); [] means Gemini genuinely
        # looked and found nothing worth keeping. Callers must not treat
        # these the same way — conflating them silently discards a batch
        # that was never actually reviewed.
        prompt = _build_note_summarization_prompt(exchanges)
        try:
            raw = self._call_generate(prompt)
        except (GeminiUnavailableError, GeminiConfigError) as exc:
            logger.warning("note summarization call failed: %s", exc)
            return None
        try:
            parsed = json.loads(_strip_code_fences(raw))
        except json.JSONDecodeError:
            logger.warning("note summarization returned non-JSON output: %r", raw)
            return None
        if not isinstance(parsed, list):
            logger.warning("note summarization returned non-array JSON: %r", parsed)
            return None
        notes = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            content = item.get("content")
            if not (isinstance(title, str) and isinstance(content, str)):
                continue
            tags = item.get("tags", "")
            tags = tags if isinstance(tags, str) else ""
            importance = item.get("importance", 5)
            if not isinstance(importance, int):
                importance = 5
            importance = max(1, min(10, importance))
            notes.append(
                {"title": title, "content": content, "tags": tags, "importance": importance}
            )
        return notes
