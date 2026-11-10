from unittest.mock import Mock

import pytest

from google.genai import errors as genai_errors

from brain.gemini_client import GeminiConfigError, GeminiUnavailableError, _call_with_retry


def test_call_with_retry_returns_value_on_first_success():
    fn = Mock(return_value="ok")

    assert _call_with_retry(fn) == "ok"
    assert fn.call_count == 1


def test_call_with_retry_retries_once_then_succeeds(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fn = Mock(side_effect=[Exception("boom"), "ok"])

    assert _call_with_retry(fn) == "ok"
    assert fn.call_count == 2


def test_call_with_retry_raises_gemini_unavailable_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fn = Mock(side_effect=Exception("boom"))

    with pytest.raises(GeminiUnavailableError):
        _call_with_retry(fn)
    assert fn.call_count == 2


def test_call_with_retry_raises_config_error_without_retrying_on_404(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    error = genai_errors.ClientError(
        404, {"error": {"message": "model not found", "status": "NOT_FOUND"}}
    )
    fn = Mock(side_effect=error)

    with pytest.raises(GeminiConfigError) as exc_info:
        _call_with_retry(fn)
    assert fn.call_count == 1
    assert exc_info.value.code == 404


def test_call_with_retry_raises_config_error_with_code_on_401(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    error = genai_errors.ClientError(
        401, {"error": {"message": "invalid API key", "status": "UNAUTHENTICATED"}}
    )
    fn = Mock(side_effect=error)

    with pytest.raises(GeminiConfigError) as exc_info:
        _call_with_retry(fn)
    assert exc_info.value.code == 401


def test_call_with_retry_raises_unavailable_without_retrying_on_429(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    error = genai_errors.ClientError(
        429,
        {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}},
    )
    fn = Mock(side_effect=error)

    with pytest.raises(GeminiUnavailableError) as exc_info:
        _call_with_retry(fn)
    assert fn.call_count == 1
    assert exc_info.value.code == 429


def test_call_with_retry_raises_unavailable_with_no_code_for_non_api_error(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fn = Mock(side_effect=Exception("boom"))

    with pytest.raises(GeminiUnavailableError) as exc_info:
        _call_with_retry(fn)
    assert exc_info.value.code is None


from brain.gemini_client import _strip_code_fences


def test_strip_code_fences_removes_json_fence():
    assert _strip_code_fences('```json\n{"a": "b"}\n```') == '{"a": "b"}'


def test_strip_code_fences_leaves_plain_text_untouched():
    assert _strip_code_fences('{"a": "b"}') == '{"a": "b"}'


from brain.gemini_client import GeminiClient


def _fake_client(response_text):
    fake = Mock()
    fake.models.generate_content.return_value = Mock(text=response_text)
    return fake


def test_generate_reply_returns_model_text_and_includes_context_in_prompt():
    fake = _fake_client("Hello there!")
    client = GeminiClient(api_key="unused", client=fake)

    reply = client.generate_reply(
        system_prompt="You are JARVIS.",
        profile="- main_editor: VS Code",
        history=[{"role": "user", "content": "hi"}],
        user_message="what's my editor?",
    )

    assert reply == "Hello there!"
    sent_prompt = fake.models.generate_content.call_args.kwargs["contents"]
    assert "what's my editor?" in sent_prompt
    assert "main_editor: VS Code" in sent_prompt


def test_extract_facts_parses_json_object_from_response():
    fake = _fake_client('{"main_editor": "Neovim"}')
    client = GeminiClient(api_key="unused", client=fake)

    facts = client.extract_facts([{"role": "user", "content": "I switched to Neovim"}])

    assert facts == {"main_editor": "Neovim"}


def test_extract_facts_parses_json_wrapped_in_code_fences():
    fake = _fake_client('```json\n{"main_editor": "Neovim"}\n```')
    client = GeminiClient(api_key="unused", client=fake)

    facts = client.extract_facts([{"role": "user", "content": "I switched to Neovim"}])

    assert facts == {"main_editor": "Neovim"}


def test_extract_facts_returns_empty_dict_on_malformed_json():
    fake = _fake_client("not valid json at all")
    client = GeminiClient(api_key="unused", client=fake)

    facts = client.extract_facts([{"role": "user", "content": "blah"}])

    assert facts == {}


def test_extract_facts_returns_empty_dict_when_response_is_a_json_array():
    fake = _fake_client("[]")
    client = GeminiClient(api_key="unused", client=fake)

    facts = client.extract_facts([{"role": "user", "content": "blah"}])

    assert facts == {}


def test_extract_facts_drops_non_string_values():
    fake = _fake_client('{"main_editor": "VS Code", "prefs": {"theme": "dark"}}')
    client = GeminiClient(api_key="unused", client=fake)

    facts = client.extract_facts([{"role": "user", "content": "blah"}])

    assert facts == {"main_editor": "VS Code"}


def test_generate_reply_raises_unavailable_when_response_text_is_none(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fake = _fake_client(None)
    client = GeminiClient(api_key="unused", client=fake)

    with pytest.raises(GeminiUnavailableError):
        client.generate_reply(
            system_prompt="You are JARVIS.",
            profile="",
            history=[],
            user_message="hi",
        )


def test_extract_facts_returns_empty_dict_when_response_text_is_none(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fake = _fake_client(None)
    client = GeminiClient(api_key="unused", client=fake)

    facts = client.extract_facts([{"role": "user", "content": "blah"}])

    assert facts == {}


def test_generate_reply_sends_plain_text_contents_when_no_image_given():
    fake = _fake_client("no image here")
    client = GeminiClient(api_key="unused", client=fake)

    client.generate_reply(
        system_prompt="You are JARVIS.",
        profile="",
        history=[],
        user_message="hi",
    )

    sent_contents = fake.models.generate_content.call_args.kwargs["contents"]
    assert isinstance(sent_contents, str)


def test_generate_reply_includes_image_part_when_image_given():
    fake = _fake_client("I see a cat.")
    client = GeminiClient(api_key="unused", client=fake)

    reply = client.generate_reply(
        system_prompt="You are JARVIS.",
        profile="",
        history=[],
        user_message="what is this?",
        image_bytes=b"fake-image-bytes",
        image_mime_type="image/png",
    )

    assert reply == "I see a cat."
    sent_contents = fake.models.generate_content.call_args.kwargs["contents"]
    assert isinstance(sent_contents, list)
    assert "what is this?" in sent_contents[0]
    image_part = sent_contents[1]
    assert image_part.inline_data.mime_type == "image/png"
    assert image_part.inline_data.data == b"fake-image-bytes"


def _fake_client_quota_exhausted():
    fake = Mock()
    error = genai_errors.ClientError(
        429,
        {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}},
    )
    fake.models.generate_content.side_effect = error
    return fake


def test_generate_reply_falls_back_to_next_model_when_first_is_exhausted(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fake = Mock()
    exhausted = genai_errors.ClientError(
        429,
        {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}},
    )
    fake.models.generate_content.side_effect = [exhausted, Mock(text="from model B")]

    client = GeminiClient(
        api_key="unused", models=["model-a", "model-b"], client=fake
    )

    reply = client.generate_reply(
        system_prompt="You are JARVIS.",
        profile="",
        history=[],
        user_message="hi",
    )

    assert reply == "from model B"
    assert fake.models.generate_content.call_count == 2
    first_call_model = fake.models.generate_content.call_args_list[0].kwargs["model"]
    second_call_model = fake.models.generate_content.call_args_list[1].kwargs["model"]
    assert first_call_model == "model-a"
    assert second_call_model == "model-b"


def test_generate_reply_raises_unavailable_when_all_models_exhausted(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fake = _fake_client_quota_exhausted()

    client = GeminiClient(
        api_key="unused", models=["model-a", "model-b"], client=fake
    )

    with pytest.raises(GeminiUnavailableError):
        client.generate_reply(
            system_prompt="You are JARVIS.",
            profile="",
            history=[],
            user_message="hi",
        )

    assert fake.models.generate_content.call_count == 2


def test_extract_facts_falls_back_to_next_model_when_first_is_exhausted(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fake = Mock()
    exhausted = genai_errors.ClientError(
        429,
        {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}},
    )
    fake.models.generate_content.side_effect = [
        exhausted,
        Mock(text='{"main_editor": "Neovim"}'),
    ]

    client = GeminiClient(
        api_key="unused", models=["model-a", "model-b"], client=fake
    )

    facts = client.extract_facts([{"role": "user", "content": "I switched to Neovim"}])

    assert facts == {"main_editor": "Neovim"}
    assert fake.models.generate_content.call_count == 2


def test_generate_reply_propagates_config_error_immediately_on_bad_api_key(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fake = Mock()
    bad_key = genai_errors.ClientError(
        401, {"error": {"message": "invalid API key", "status": "UNAUTHENTICATED"}}
    )
    fake.models.generate_content.side_effect = bad_key

    client = GeminiClient(api_key="unused", models=["model-a", "model-b"], client=fake)

    with pytest.raises(GeminiConfigError):
        client.generate_reply(
            system_prompt="You are JARVIS.",
            profile="",
            history=[],
            user_message="hi",
        )

    # A bad API key is a global problem, not per-model — must not cascade.
    assert fake.models.generate_content.call_count == 1


def test_generate_reply_cascades_past_404_to_next_model(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fake = Mock()
    not_found = genai_errors.ClientError(
        404, {"error": {"message": "model not found", "status": "NOT_FOUND"}}
    )
    fake.models.generate_content.side_effect = [not_found, Mock(text="from model B")]

    client = GeminiClient(api_key="unused", models=["model-a", "model-b"], client=fake)

    reply = client.generate_reply(
        system_prompt="You are JARVIS.",
        profile="",
        history=[],
        user_message="hi",
    )

    assert reply == "from model B"
    assert fake.models.generate_content.call_count == 2


import datetime

from brain.gemini_client import _seconds_until_quota_reset


def test_seconds_until_quota_reset_counts_to_pacific_midnight_in_winter():
    # 12:00 UTC in January is 04:00 PST (UTC-8); quota resets at 08:00 UTC.
    noon_utc = datetime.datetime(
        2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc
    ).timestamp()

    seconds = _seconds_until_quota_reset(noon_utc)

    assert seconds == pytest.approx(20 * 3600, abs=1)


def test_seconds_until_quota_reset_follows_daylight_saving_in_summer():
    # 06:59 UTC in July is 23:59 PDT (UTC-7); quota resets a minute later.
    almost_reset = datetime.datetime(
        2026, 7, 1, 6, 59, 0, tzinfo=datetime.timezone.utc
    ).timestamp()

    seconds = _seconds_until_quota_reset(almost_reset)

    assert seconds == pytest.approx(60, abs=1)


def test_seconds_until_quota_reset_is_not_utc_midnight():
    # 00:30 UTC is 17:30 PDT the previous day — reset is 6.5h away, not
    # the 23.5h a UTC-midnight reset would imply.
    just_after_utc_midnight = datetime.datetime(
        2026, 7, 1, 0, 30, 0, tzinfo=datetime.timezone.utc
    ).timestamp()

    seconds = _seconds_until_quota_reset(just_after_utc_midnight)

    assert seconds == pytest.approx(6.5 * 3600, abs=1)


def test_generate_reply_skips_model_still_in_cooldown_from_previous_failure(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fake = Mock()
    exhausted = genai_errors.ClientError(
        429, {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}}
    )
    fake.models.generate_content.side_effect = [
        exhausted,
        Mock(text="from model B, call 1"),
        Mock(text="from model B, call 2"),
    ]

    fake_now = [1000.0]
    client = GeminiClient(
        api_key="unused",
        models=["model-a", "model-b"],
        client=fake,
        now_fn=lambda: fake_now[0],
    )

    first = client.generate_reply(system_prompt="p", profile="", history=[], user_message="hi")
    second = client.generate_reply(
        system_prompt="p", profile="", history=[], user_message="hi again"
    )

    assert first == "from model B, call 1"
    assert second == "from model B, call 2"
    assert fake.models.generate_content.call_count == 3
    models_called = [
        c.kwargs["model"] for c in fake.models.generate_content.call_args_list
    ]
    assert models_called == ["model-a", "model-b", "model-b"]


def test_generate_reply_retries_model_after_transient_cooldown_expires(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fake = Mock()
    overloaded = genai_errors.ClientError(
        503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}}
    )
    fake.models.generate_content.side_effect = [
        overloaded,
        Mock(text="from model B"),
        Mock(text="model-a is back"),
    ]

    fake_now = [1000.0]
    client = GeminiClient(
        api_key="unused",
        models=["model-a", "model-b"],
        client=fake,
        now_fn=lambda: fake_now[0],
    )

    client.generate_reply(system_prompt="p", profile="", history=[], user_message="hi")
    fake_now[0] += 10_000  # well past the short transient cooldown
    reply = client.generate_reply(
        system_prompt="p", profile="", history=[], user_message="hi again"
    )

    assert reply == "model-a is back"
    models_called = [
        c.kwargs["model"] for c in fake.models.generate_content.call_args_list
    ]
    assert models_called == ["model-a", "model-b", "model-a"]


def test_generate_reply_does_not_sleep_before_moving_to_next_model_on_503(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: sleep_calls.append(s))
    fake = Mock()
    overloaded = genai_errors.ClientError(
        503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}}
    )
    fake.models.generate_content.side_effect = [overloaded, Mock(text="from model B")]

    client = GeminiClient(api_key="unused", models=["model-a", "model-b"], client=fake)

    reply = client.generate_reply(system_prompt="p", profile="", history=[], user_message="hi")

    assert reply == "from model B"
    assert sleep_calls == []
    assert fake.models.generate_content.call_count == 2


def test_generate_reply_retries_same_model_once_when_only_one_model_configured(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fake = Mock()
    overloaded = genai_errors.ClientError(
        503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}}
    )
    fake.models.generate_content.side_effect = [overloaded, Mock(text="succeeded on retry")]

    client = GeminiClient(api_key="unused", models=["only-model"], client=fake)

    reply = client.generate_reply(system_prompt="p", profile="", history=[], user_message="hi")

    assert reply == "succeeded on retry"
    assert fake.models.generate_content.call_count == 2


from brain.gemini_client import ToolReply


def _fake_tool_response(text=None, function_calls=None):
    resp = Mock()
    resp.text = text
    resp.function_calls = function_calls
    return resp


def _fake_function_call(name, args):
    fc = Mock()
    fc.name = name
    fc.args = args
    return fc


_KNOWN_ACTION_TOOL = {
    "name": "run_known_action",
    "description": "Run a known action.",
    "parameters_json_schema": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
}


def test_generate_with_tools_returns_function_call_when_model_calls_one():
    fake = Mock()
    fake.models.generate_content.return_value = _fake_tool_response(
        text=None,
        function_calls=[_fake_function_call("run_known_action", {"name": "vscode"})],
    )
    client = GeminiClient(api_key="unused", client=fake)

    reply = client.generate_with_tools(
        system_prompt="You are JARVIS.",
        profile="",
        history=[],
        user_message="open vscode",
        tools=[_KNOWN_ACTION_TOOL],
    )

    assert isinstance(reply, ToolReply)
    assert reply.function_name == "run_known_action"
    assert reply.function_args == {"name": "vscode"}


def test_generate_with_tools_returns_plain_text_when_no_function_called():
    fake = Mock()
    fake.models.generate_content.return_value = _fake_tool_response(
        text="Hello there!", function_calls=None
    )
    client = GeminiClient(api_key="unused", client=fake)

    reply = client.generate_with_tools(
        system_prompt="You are JARVIS.",
        profile="",
        history=[],
        user_message="hi",
        tools=[_KNOWN_ACTION_TOOL],
    )

    assert reply.text == "Hello there!"
    assert reply.function_name is None
    assert reply.function_args is None


def test_generate_with_tools_falls_back_to_next_model_when_first_is_exhausted(monkeypatch):
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fake = Mock()
    exhausted = genai_errors.ClientError(
        429, {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}}
    )
    fake.models.generate_content.side_effect = [
        exhausted,
        _fake_tool_response(text="from model B", function_calls=None),
    ]
    client = GeminiClient(api_key="unused", models=["model-a", "model-b"], client=fake)

    reply = client.generate_with_tools(
        system_prompt="p", profile="", history=[], user_message="hi", tools=[_KNOWN_ACTION_TOOL]
    )

    assert reply.text == "from model B"
    assert fake.models.generate_content.call_count == 2


def test_generate_with_tools_handles_function_call_with_no_arguments():
    fake = Mock()
    fake.models.generate_content.return_value = _fake_tool_response(
        text=None,
        function_calls=[_fake_function_call("run_known_action", None)],
    )
    client = GeminiClient(api_key="unused", client=fake)

    reply = client.generate_with_tools(
        system_prompt="You are JARVIS.",
        profile="",
        history=[],
        user_message="open something",
        tools=[_KNOWN_ACTION_TOOL],
    )

    assert reply.function_name == "run_known_action"
    assert reply.function_args == {}


def _fake_embedding_client(values):
    fake = Mock()
    fake.models.embed_content.return_value = Mock(embeddings=[Mock(values=values)])
    return fake


def test_embed_text_returns_the_embedding_values():
    fake = _fake_embedding_client([0.1, 0.2, 0.3])
    client = GeminiClient(api_key="unused", client=fake)

    result = client.embed_text("hello world")

    assert result == [0.1, 0.2, 0.3]


def test_embed_text_requests_768_dimensions():
    fake = _fake_embedding_client([0.0] * 768)
    client = GeminiClient(api_key="unused", client=fake)

    client.embed_text("hello world")

    call_kwargs = fake.models.embed_content.call_args.kwargs
    assert call_kwargs["config"].output_dimensionality == 768


def test_embed_text_raises_gemini_unavailable_when_no_embeddings_returned():
    fake = Mock()
    fake.models.embed_content.return_value = Mock(embeddings=[])
    client = GeminiClient(api_key="unused", client=fake)

    with pytest.raises(GeminiUnavailableError):
        client.embed_text("hello world")


def test_summarize_for_notes_parses_json_array_of_note_candidates():
    fake = _fake_client(
        '[{"title": "Trading setup", "content": "Configured automation.", '
        '"tags": "trading", "importance": 7}]'
    )
    client = GeminiClient(api_key="unused", client=fake)

    notes = client.summarize_for_notes(
        [{"role": "user", "content": "let's set up the trading journal"}]
    )

    assert notes == [
        {"title": "Trading setup", "content": "Configured automation.",
         "tags": "trading", "importance": 7}
    ]


def test_summarize_for_notes_returns_empty_list_when_nothing_worth_keeping():
    fake = _fake_client("[]")
    client = GeminiClient(api_key="unused", client=fake)

    notes = client.summarize_for_notes([{"role": "user", "content": "hi"}])

    assert notes == []


def test_summarize_for_notes_returns_none_on_malformed_json():
    fake = _fake_client("not json")
    client = GeminiClient(api_key="unused", client=fake)

    notes = client.summarize_for_notes([{"role": "user", "content": "hi"}])

    assert notes is None


def test_summarize_for_notes_clamps_importance_and_defaults_when_missing_or_invalid():
    fake = _fake_client(
        '[{"title": "A", "content": "B", "tags": "", "importance": 99}, '
        '{"title": "C", "content": "D", "tags": ""}]'
    )
    client = GeminiClient(api_key="unused", client=fake)

    notes = client.summarize_for_notes([{"role": "user", "content": "hi"}])

    assert notes[0]["importance"] == 10
    assert notes[1]["importance"] == 5


def test_summarize_for_notes_returns_none_when_gemini_unavailable(monkeypatch):
    fake = Mock()
    fake.models.generate_content.side_effect = Exception("down")
    client = GeminiClient(api_key="unused", client=fake)
    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)

    notes = client.summarize_for_notes([{"role": "user", "content": "hi"}])

    assert notes is None


def test_summarize_for_notes_returns_none_on_non_array_json():
    fake = _fake_client('{"not": "an array"}')
    client = GeminiClient(api_key="unused", client=fake)

    notes = client.summarize_for_notes([{"role": "user", "content": "hi"}])

    assert notes is None


def test_answer_with_tool_result_sends_tool_output_and_returns_model_text():
    from brain.gemini_client import GeminiClient

    fake = Mock()
    fake.models.generate_content.return_value = Mock(text="It's sunny in Tokyo, sir.")
    client = GeminiClient(api_key="unused", models=["model-a"], client=fake)

    reply = client.answer_with_tool_result(
        system_prompt="You are JARVIS.",
        profile="",
        history=[],
        user_message="weather in Tokyo?",
        tool_name="get_weather",
        tool_result="Tokyo: 24C, clear",
    )

    assert reply == "It's sunny in Tokyo, sir."
    prompt_sent = fake.models.generate_content.call_args.kwargs["contents"]
    assert "get_weather" in prompt_sent
    assert "Tokyo: 24C, clear" in prompt_sent
    assert "weather in Tokyo?" in prompt_sent
    # A plain generation call — no tools offered, so it can't loop.
    assert "config" not in fake.models.generate_content.call_args.kwargs


def test_answer_with_tool_result_tells_model_to_fall_back_on_its_own_knowledge():
    from brain.gemini_client import GeminiClient

    fake = Mock()
    fake.models.generate_content.return_value = Mock(text="145 days, sir.")
    client = GeminiClient(api_key="unused", models=["model-a"], client=fake)

    client.answer_with_tool_result(
        system_prompt="p", profile="", history=[], user_message="days since May 1?",
        tool_name="search_memory", tool_result="(no matching notes)",
    )

    prompt_sent = fake.models.generate_content.call_args.kwargs["contents"]
    assert "own knowledge" in prompt_sent.lower()


def test_answer_with_tool_result_includes_image_when_given():
    from brain.gemini_client import GeminiClient

    fake = Mock()
    fake.models.generate_content.return_value = Mock(text="ok")
    client = GeminiClient(api_key="unused", models=["model-a"], client=fake)

    client.answer_with_tool_result(
        system_prompt="p", profile="", history=[], user_message="what is this?",
        tool_name="web_search", tool_result="results",
        image_bytes=b"\x89PNG", image_mime_type="image/png",
    )

    contents = fake.models.generate_content.call_args.kwargs["contents"]
    assert isinstance(contents, list) and len(contents) == 2


def test_answer_with_tool_result_raises_unavailable_when_all_models_fail(monkeypatch):
    from brain.gemini_client import GeminiClient

    monkeypatch.setattr("brain.gemini_client.time.sleep", lambda s: None)
    fake = Mock()
    fake.models.generate_content.side_effect = Exception("boom")
    client = GeminiClient(api_key="unused", models=["model-a"], client=fake)

    with pytest.raises(GeminiUnavailableError):
        client.answer_with_tool_result(
            system_prompt="p", profile="", history=[], user_message="hi",
            tool_name="web_search", tool_result="results",
        )


def test_generate_with_tools_keeps_every_function_call():
    fake = Mock()
    fake.models.generate_content.return_value = _fake_tool_response(
        text=None,
        function_calls=[
            _fake_function_call("create_reminder", {"text": "Linux"}),
            _fake_function_call("create_reminder", {"text": "Networking"}),
        ],
    )
    client = GeminiClient(api_key="unused", client=fake)

    reply = client.generate_with_tools(
        system_prompt="You are JARVIS.", profile="", history=[],
        user_message="add 2 reminders", tools=[_KNOWN_ACTION_TOOL],
    )

    assert reply.calls == [
        ("create_reminder", {"text": "Linux"}),
        ("create_reminder", {"text": "Networking"}),
    ]
    assert reply.function_name == "create_reminder"
    assert reply.function_args == {"text": "Linux"}


def test_tool_reply_built_with_one_call_lists_it_in_calls():
    assert ToolReply(text=None, function_name="x", function_args={"a": 1}).calls == [("x", {"a": 1})]
    assert ToolReply(text="hi", function_name=None, function_args=None).calls == []
