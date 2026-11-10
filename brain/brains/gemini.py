import logging

from brain.brains import FAIL_CONFIG, FAIL_UNAVAILABLE, BrainResult, TurnContext
from brain.gemini_client import GeminiConfigError, GeminiUnavailableError
from brain.tools import ToolOutcome

logger = logging.getLogger(__name__)

# Gemini can ask for several tool calls in one reply ("add 2 reminders");
# more than this in a single reply is almost certainly a runaway.
MAX_TOOL_CALLS_PER_REPLY = 10


class GeminiBrain:
    name = "gemini"

    def __init__(self, client):
        self.client = client

    def respond(self, ctx: TurnContext) -> BrainResult:
        try:
            tool_reply = self.client.generate_with_tools(
                ctx.system_prompt,
                ctx.profile,
                ctx.history,
                ctx.message,
                ctx.tools,
                image_bytes=ctx.image_bytes,
                image_mime_type=ctx.image_mime_type,
            )
        except GeminiConfigError as exc:
            return BrainResult(None, self.name, failure=FAIL_CONFIG, failure_detail=str(exc))
        except GeminiUnavailableError as exc:
            return BrainResult(
                None, self.name, failure=FAIL_UNAVAILABLE, failure_detail=str(exc)
            )

        if tool_reply.function_name is None:
            if not tool_reply.text:
                return BrainResult(None, self.name, failure=FAIL_UNAVAILABLE)
            return BrainResult(tool_reply.text, self.name)

        calls = tool_reply.calls[:MAX_TOOL_CALLS_PER_REPLY]
        if len(calls) > 1:
            return self._respond_to_several(ctx, tool_reply, calls)

        outcome = ctx.executor.execute(
            tool_reply.function_name, tool_reply.function_args, model_text=tool_reply.text
        )

        if outcome.kind == "action":
            text = tool_reply.text or outcome.text
        elif outcome.kind == "fixed":
            text = outcome.text
        else:  # information
            text = self._phrase(ctx, tool_reply.function_name, outcome)

        return BrainResult(
            text,
            self.name,
            tools_ran=[tool_reply.function_name],
            side_effect_tools_ran=(
                [] if outcome.kind == "information" else [tool_reply.function_name]
            ),
            handled_as_note=outcome.handled_as_note,
        )

    def _respond_to_several(self, ctx: TurnContext, tool_reply, calls) -> BrainResult:
        lines: list[str] = []
        info: list[tuple[str, object]] = []
        side_effects: list[str] = []
        handled_as_note = False
        for name, args in calls:
            outcome = ctx.executor.execute(name, args, model_text=tool_reply.text)
            handled_as_note = handled_as_note or outcome.handled_as_note
            if outcome.kind == "information":
                info.append((name, outcome))
            else:
                lines.append(outcome.text)
                side_effects.append(name)
        if info:
            if len(info) == 1:
                name, outcome = info[0]
                lines.append(self._phrase(ctx, name, outcome))
            else:
                combined = ToolOutcome(
                    "\n\n".join(f"{n}:\n{o.text}" for n, o in info),
                    "information",
                    fallback="\n\n".join(o.fallback or o.text for _, o in info),
                )
                lines.append(self._phrase(ctx, ", ".join(n for n, _ in info), combined))
        return BrainResult(
            "\n".join(lines),
            self.name,
            tools_ran=[name for name, _ in calls],
            side_effect_tools_ran=side_effects,
            handled_as_note=handled_as_note,
        )

    def _phrase(self, ctx: TurnContext, tool_name: str, outcome) -> str:
        try:
            return self.client.answer_with_tool_result(
                system_prompt=ctx.system_prompt,
                profile=ctx.profile,
                history=ctx.history,
                user_message=ctx.message,
                tool_name=tool_name,
                tool_result=outcome.text,
                image_bytes=ctx.image_bytes,
                image_mime_type=ctx.image_mime_type,
            )
        except (GeminiUnavailableError, GeminiConfigError) as exc:
            logger.warning("answering from %s result failed: %s", tool_name, exc)
            return outcome.fallback
