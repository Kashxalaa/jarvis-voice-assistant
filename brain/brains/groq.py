import logging

from groq import APITimeoutError, Groq, RateLimitError

from brain.brains import FAIL_UNAVAILABLE, BrainResult, TurnContext
from brain.gemini_client import _build_conversation_prompt

logger = logging.getLogger(__name__)

# GroqBrain is a text-only last resort inside Gemini's fallback chain, not
# the fast judge call (which uses its own 4s timeout) — give it more room
# since it's answering the user's actual message.
_RESPOND_TIMEOUT = 15.0


class GroqUnavailable(Exception):
    """kind is "timeout", "rate_limit" or "other"."""

    def __init__(self, message: str = "", kind: str = "other"):
        super().__init__(message)
        self.kind = kind


def _classify(exc: Exception) -> str:
    if isinstance(exc, APITimeoutError):
        return "timeout"
    if isinstance(exc, RateLimitError):
        return "rate_limit"
    return "other"


def groq_chat(
    api_key: str, model: str, messages: list[dict], *, json_mode: bool, timeout: float,
    reasoning_effort: str | None = None,
) -> str:
    kwargs = {}
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        client = Groq(api_key=api_key, timeout=timeout, max_retries=0)
        response = client.chat.completions.create(
            model=model, messages=messages, temperature=0, **kwargs
        )
        content = response.choices[0].message.content
    except GroqUnavailable:
        raise
    except Exception as exc:
        raise GroqUnavailable(str(exc), kind=_classify(exc)) from exc

    if not content:
        raise GroqUnavailable("Groq returned an empty response")
    return content


class GroqBrain:
    name = "groq"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def respond(self, ctx: TurnContext) -> BrainResult:
        prompt = _build_conversation_prompt(
            ctx.system_prompt, ctx.profile, ctx.history, ctx.message
        )
        messages = [{"role": "user", "content": prompt}]

        try:
            text = groq_chat(
                self.api_key, self.model, messages, json_mode=False, timeout=_RESPOND_TIMEOUT
            )
        except GroqUnavailable as exc:
            logger.warning("Groq brain unavailable: %s", exc)
            return BrainResult(None, self.name, failure=FAIL_UNAVAILABLE, failure_detail=str(exc))

        if not text:
            return BrainResult(None, self.name, failure=FAIL_UNAVAILABLE)

        return BrainResult(text, self.name)
