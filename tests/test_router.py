import json

import pytest

from brain.router import route


def _judge(monkeypatch, payload=None, exc=None):
    import brain.router as router

    def fake(api_key, model, messages, *, json_mode, timeout, **kw):
        if exc:
            raise exc
        return payload if isinstance(payload, str) else json.dumps(payload)

    monkeypatch.setattr(router, "groq_chat", fake)


def R(msg, **kw):
    args = dict(has_image=False, history=[], has_pending=False, groq_api_key="k", groq_model="m")
    args.update(kw)
    return route(msg, **args)


def test_small_talk(monkeypatch):
    _judge(monkeypatch, {"tier": "small_talk", "reply": "Good night, sir."})
    d = R("good night")
    assert (d.tier, d.small_talk_reply) == ("small_talk", "Good night, sir.")


def test_hard_and_normal(monkeypatch):
    _judge(monkeypatch, {"tier": "hard"})
    assert R("plan my week").tier == "hard"
    _judge(monkeypatch, {"tier": "normal"})
    assert R("remind me at 9").tier == "normal"


@pytest.mark.parametrize("payload", ["not json", {"tier": "genius"}, {"tier": "small_talk", "reply": ""},
                                     {"tier": "small_talk", "reply": "x" * 401}, {}])
def test_bad_judge_output_routes_normal(monkeypatch, payload):
    _judge(monkeypatch, payload)
    d = R("hi")
    assert (d.tier, d.reason) == ("normal", "judge_failed")


def test_judge_failure_routes_normal(monkeypatch):
    from brain.brains.groq import GroqUnavailable

    _judge(monkeypatch, exc=GroqUnavailable("down"))
    assert R("hi").tier == "normal"


def test_override_strips_phrase_and_skips_judge(monkeypatch):
    _judge(monkeypatch, exc=AssertionError("judge must not be called"))
    d = R("use claude: compare these two laptops")
    assert (d.tier, d.reason, d.cleaned_message) == ("hard", "override", "compare these two laptops")
    assert R("Use Gemini what's 2+2").tier == "normal"


def test_override_only_message_keeps_original(monkeypatch):
    _judge(monkeypatch, exc=AssertionError("no judge"))
    assert R("use claude").cleaned_message == "use claude"


def test_no_key_routes_normal(monkeypatch):
    _judge(monkeypatch, exc=AssertionError("no judge"))
    assert R("hi", groq_api_key=None).tier == "normal"


def test_image_floor(monkeypatch):
    _judge(monkeypatch, {"tier": "small_talk", "reply": "nice"})
    assert R("", has_image=True).tier == "normal"


def test_pending_proposal_forces_at_least_normal(monkeypatch):
    _judge(monkeypatch, {"tier": "small_talk", "reply": "ok!"})
    d = R("yes", has_pending=True)
    assert (d.tier, d.reason) == ("normal", "pending")


def test_judge_prompt_is_small(monkeypatch):
    seen = {}
    import brain.router as router

    def fake(api_key, model, messages, *, json_mode, timeout, **kw):
        seen["chars"] = sum(len(m["content"]) for m in messages)
        seen["timeout"] = timeout
        return json.dumps({"tier": "normal"})

    monkeypatch.setattr(router, "groq_chat", fake)
    hist = [{"role": "user", "content": "x" * 5000}] * 10
    R("y" * 5000, history=hist)
    assert seen["chars"] < 4000 and seen["timeout"] == 4


def test_long_message_is_truncated_with_marker(monkeypatch):
    seen = {}
    import brain.router as router

    def fake(api_key, model, messages, *, json_mode, timeout, **kw):
        seen["user_content"] = messages[1]["content"]
        return json.dumps({"tier": "normal"})

    monkeypatch.setattr(router, "groq_chat", fake)
    R("z" * 5000)
    assert "…[message truncated — it is longer than shown]" in seen["user_content"]


def test_override_mid_sentence_collapses_whitespace(monkeypatch):
    _judge(monkeypatch, exc=AssertionError("judge must not be called"))
    d = R("please use claude to compare X and Y")
    assert d.tier == "hard"
    assert "  " not in d.cleaned_message


def test_reply_floors_small_talk_to_normal(monkeypatch):
    _judge(monkeypatch, {"tier": "small_talk", "reply": "ok!"})
    d = R("yes", is_reply=True)
    assert (d.tier, d.reason, d.small_talk_reply) == ("normal", "reply", None)


def test_judge_prompt_has_confirmation_rule():
    from brain.router import JUDGE_PROMPT

    assert "responds to or confirms something Jarvis just asked or offered" in JUDGE_PROMPT
    assert "is normal, not small_talk" in JUDGE_PROMPT


def test_judge_called_with_low_reasoning_effort(monkeypatch):
    import brain.router as router

    seen = {}

    def fake(api_key, model, messages, *, json_mode, timeout, **kw):
        seen.update(kw)
        return json.dumps({"tier": "normal"})

    monkeypatch.setattr(router, "groq_chat", fake)
    R("hi")
    assert seen["reasoning_effort"] == "low"


def test_rate_limit_pauses_judge_for_ten_minutes(monkeypatch):
    import brain.router as router
    from brain.brains.groq import GroqUnavailable

    now = [1000.0]
    monkeypatch.setattr(router.time, "monotonic", lambda: now[0])
    calls = []

    def fake(api_key, model, messages, *, json_mode, timeout, **kw):
        calls.append(1)
        raise GroqUnavailable("429", kind="rate_limit")

    monkeypatch.setattr(router, "groq_chat", fake)
    assert R("a").reason == "judge_failed"
    assert R("b").reason == "judge_failed"
    assert len(calls) == 1
    now[0] += 601
    R("c")
    assert len(calls) == 2


def test_judge_failure_warning_logged_once_per_window(monkeypatch, caplog):
    import logging

    import brain.router as router
    from brain.brains.groq import GroqUnavailable

    now = [1000.0]
    monkeypatch.setattr(router.time, "monotonic", lambda: now[0])
    _judge(monkeypatch, exc=GroqUnavailable("slow", kind="timeout"))
    with caplog.at_level(logging.WARNING, logger="brain.router"):
        R("a")
        R("b")
        assert len([r for r in caplog.records if "timeout" in r.getMessage()]) == 1
        now[0] += 601
        R("c")
    assert len([r for r in caplog.records if "timeout" in r.getMessage()]) == 2


def test_bad_json_warning_logged(monkeypatch, caplog):
    import logging

    _judge(monkeypatch, "not json")
    with caplog.at_level(logging.WARNING, logger="brain.router"):
        R("a")
    assert any("bad_json" in r.getMessage() for r in caplog.records)


def test_judge_prompt_forbids_claiming_actions():
    from brain.router import JUDGE_PROMPT
    assert "never claim" in JUDGE_PROMPT.lower()


@pytest.mark.parametrize("reply", [
    "Understood, sir; testing noted.",
    "Saved, sir.",
    "I'll remember that, sir.",
    "Reminder set for 5, sir.",
    "Added to your list, sir.",
    "Done, sir — scheduled.",
])
def test_small_talk_reply_that_claims_an_action_routes_normal(monkeypatch, reply):
    _judge(monkeypatch, {"tier": "small_talk", "reply": reply})
    d = R("just testing")
    assert (d.tier, d.reason) == ("normal", "judge_failed")


def test_plain_small_talk_reply_is_kept(monkeypatch):
    _judge(monkeypatch, {"tier": "small_talk", "reply": "Good to hear from you, sir."})
    assert R("just testing").tier == "small_talk"


def test_judge_prompt_sends_computer_requests_to_hard():
    from brain.router import JUDGE_PROMPT

    assert "hard also: anything about the computer itself" in JUDGE_PROMPT
    for word in ("files", "folders", "documents", "programs", "shell commands", "installs",
                 "the server", "logs", "disk space", "on my PC"):
        assert word in JUDGE_PROMPT


# ---- staying with Claude in the middle of its conversation ----------------------

def test_a_follow_up_to_claude_stays_with_claude(monkeypatch):
    _judge(monkeypatch, {"tier": "normal"})
    d = R("yes", continues_claude=True)
    assert (d.tier, d.reason) == ("hard", "continues")


def test_small_talk_after_claude_stays_small_talk(monkeypatch):
    _judge(monkeypatch, {"tier": "small_talk", "reply": "You're welcome, sir."})
    assert R("thanks", continues_claude=True).tier == "small_talk"


def test_use_gemini_still_wins_over_continuing_with_claude(monkeypatch):
    _judge(monkeypatch, {"tier": "normal"})
    assert R("use gemini: yes", continues_claude=True).tier == "normal"
