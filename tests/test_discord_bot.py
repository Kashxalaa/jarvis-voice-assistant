import threading

from discord_bot import DISCORD_MESSAGE_LIMIT, chunk_message, should_respond


def test_should_respond_true_for_owner_in_allowed_channel():
    assert should_respond(
        channel_id=111,
        author_id=222,
        author_is_bot=False,
        allowed_channel_id=111,
        owner_id=222,
    ) is True


def test_should_respond_false_for_wrong_channel():
    assert should_respond(
        channel_id=999,
        author_id=222,
        author_is_bot=False,
        allowed_channel_id=111,
        owner_id=222,
    ) is False


def test_should_respond_false_for_wrong_author_in_the_right_channel():
    assert should_respond(
        channel_id=111,
        author_id=999,
        author_is_bot=False,
        allowed_channel_id=111,
        owner_id=222,
    ) is False


def test_should_respond_false_for_bot_message():
    assert should_respond(
        channel_id=111,
        author_id=222,
        author_is_bot=True,
        allowed_channel_id=111,
        owner_id=222,
    ) is False


def test_chunk_message_returns_single_chunk_when_under_limit():
    assert chunk_message("hello") == ["hello"]


def test_chunk_message_returns_one_empty_chunk_for_empty_text():
    assert chunk_message("") == [""]


def test_chunk_message_splits_text_longer_than_limit():
    text = "a" * 2500

    chunks = chunk_message(text, limit=2000)

    assert len(chunks) == 2
    assert len(chunks[0]) == 2000
    assert len(chunks[1]) == 500
    assert "".join(chunks) == text


def test_chunk_message_handles_exact_boundary():
    text = "a" * DISCORD_MESSAGE_LIMIT

    chunks = chunk_message(text)

    assert chunks == [text]


import pytest


def test_resolve_discord_config_returns_token_channel_and_owner(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok123")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "111")
    monkeypatch.setenv("DISCORD_OWNER_ID", "222")

    from discord_bot import resolve_discord_config

    token, channel_id, owner_id = resolve_discord_config()

    assert token == "tok123"
    assert channel_id == 111
    assert owner_id == 222


def test_resolve_discord_config_raises_when_token_missing(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "111")
    monkeypatch.setenv("DISCORD_OWNER_ID", "222")

    from discord_bot import resolve_discord_config

    with pytest.raises(ValueError, match="DISCORD_BOT_TOKEN"):
        resolve_discord_config()


def test_resolve_discord_config_raises_when_channel_id_not_numeric(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok123")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "not-a-number")
    monkeypatch.setenv("DISCORD_OWNER_ID", "222")

    from discord_bot import resolve_discord_config

    with pytest.raises(ValueError, match="DISCORD_CHANNEL_ID"):
        resolve_discord_config()


def test_resolve_discord_config_raises_when_owner_id_not_numeric(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok123")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "111")
    monkeypatch.setenv("DISCORD_OWNER_ID", "not-a-number")

    from discord_bot import resolve_discord_config

    with pytest.raises(ValueError, match="DISCORD_OWNER_ID"):
        resolve_discord_config()


import discord_bot


def test_main_exits_when_api_key_missing(monkeypatch, capsys):
    monkeypatch.setattr(discord_bot, "resolve_api_key", lambda: None)

    with pytest.raises(SystemExit) as exc_info:
        discord_bot.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "GEMINI_API_KEY is not set" in captured.out


def test_main_exits_when_discord_config_invalid(monkeypatch, capsys):
    monkeypatch.setattr(discord_bot, "resolve_api_key", lambda: "key123")

    def raise_config_error():
        raise ValueError("DISCORD_BOT_TOKEN is not set")

    monkeypatch.setattr(discord_bot, "resolve_discord_config", raise_config_error)

    with pytest.raises(SystemExit) as exc_info:
        discord_bot.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Discord configuration problem" in captured.out


def test_main_exits_when_models_config_invalid(monkeypatch, capsys):
    monkeypatch.setattr(discord_bot, "resolve_api_key", lambda: "key123")
    monkeypatch.setattr(
        discord_bot, "resolve_discord_config", lambda: ("tok", 111, 222)
    )

    def raise_config_error():
        raise ValueError("GEMINI_MODELS is set but contains no model names")

    monkeypatch.setattr(discord_bot, "resolve_models", raise_config_error)

    with pytest.raises(SystemExit) as exc_info:
        discord_bot.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "GEMINI_MODELS" in captured.out


def test_main_wires_default_model_chain_into_gemini_client(monkeypatch):
    monkeypatch.setattr(discord_bot, "resolve_api_key", lambda: "key123")
    monkeypatch.setattr(
        discord_bot, "resolve_discord_config", lambda: ("tok", 111, 222)
    )
    monkeypatch.setattr(discord_bot, "resolve_database_url", lambda: "postgresql://fake")
    monkeypatch.setattr(discord_bot, "init_db", lambda url: "conn")
    monkeypatch.setattr(discord_bot, "resolve_instance_role", lambda: "local")

    captured = {}

    class FakeClient:
        def __init__(
            self, conn, gemini_client, allowed_channel_id, owner_id, database_url,
            instance_role, brains=None,
        ):
            captured["models"] = gemini_client.models

        def run(self, token):
            pass

    monkeypatch.setattr(discord_bot, "JarvisDiscordClient", FakeClient)

    discord_bot.main()

    import config

    assert captured["models"] == config.DEFAULT_MODEL_CHAIN


def _patch_main_prereqs(monkeypatch):
    monkeypatch.setattr(discord_bot, "resolve_api_key", lambda: "key123")
    monkeypatch.setattr(discord_bot, "resolve_discord_config", lambda: ("tok", 111, 222))
    monkeypatch.setattr(discord_bot, "resolve_database_url", lambda: "postgresql://fake")
    monkeypatch.setattr(discord_bot, "init_db", lambda url: "conn")
    monkeypatch.setattr(discord_bot, "resolve_instance_role", lambda: "local")


def test_main_builds_brains_and_forwards_to_client(monkeypatch):
    _patch_main_prereqs(monkeypatch)
    monkeypatch.setattr(discord_bot, "resolve_groq_api_key", lambda: "groq-key")
    monkeypatch.setattr(discord_bot, "resolve_groq_model", lambda: "groq-model")
    monkeypatch.setattr(discord_bot, "resolve_claude_model", lambda: "sonnet")
    monkeypatch.setattr(discord_bot, "resolve_claude_oauth_token", lambda: "oauth-tok")
    monkeypatch.setattr(discord_bot, "resolve_claude_cli_path", lambda: "C:/claude.exe")
    monkeypatch.setattr(discord_bot, "resolve_show_brain", lambda: False)

    captured = {}

    class FakeClient:
        def __init__(
            self, conn, gemini_client, allowed_channel_id, owner_id, database_url,
            instance_role, brains=None,
        ):
            captured["brains"] = brains

        def run(self, token):
            pass

    monkeypatch.setattr(discord_bot, "JarvisDiscordClient", FakeClient)

    discord_bot.main()

    from brain.brains.claude import ClaudeBrain
    from brain.brains.gemini import GeminiBrain
    from brain.brains.groq import GroqBrain
    from brain.conversation import Brains

    brains = captured["brains"]
    assert isinstance(brains, Brains)
    assert isinstance(brains.gemini, GeminiBrain)
    assert isinstance(brains.claude, ClaudeBrain)
    assert brains.claude.oauth_token == "oauth-tok"
    assert brains.claude.model == "sonnet"
    assert brains.claude.cli_path == "C:/claude.exe"
    assert isinstance(brains.groq, GroqBrain)
    assert brains.groq_api_key == "groq-key"
    assert brains.groq_model == "groq-model"
    assert brains.show_tag is False


def test_main_leaves_groq_none_when_key_missing(monkeypatch):
    _patch_main_prereqs(monkeypatch)
    monkeypatch.setattr(discord_bot, "resolve_groq_api_key", lambda: None)
    monkeypatch.setattr(discord_bot, "resolve_groq_model", lambda: "groq-model")
    monkeypatch.setattr(discord_bot, "resolve_claude_model", lambda: "sonnet")
    monkeypatch.setattr(discord_bot, "resolve_claude_oauth_token", lambda: None)
    monkeypatch.setattr(discord_bot, "resolve_show_brain", lambda: True)

    captured = {}

    class FakeClient:
        def __init__(
            self, conn, gemini_client, allowed_channel_id, owner_id, database_url,
            instance_role, brains=None,
        ):
            captured["brains"] = brains

        def run(self, token):
            pass

    monkeypatch.setattr(discord_bot, "JarvisDiscordClient", FakeClient)

    discord_bot.main()

    brains = captured["brains"]
    assert brains.groq is None
    assert brains.claude.oauth_token is None
    assert brains.show_tag is True


from discord_bot import _find_image_attachment


class _FakeAttachment:
    def __init__(self, content_type):
        self.content_type = content_type


def test_find_image_attachment_returns_first_image():
    attachments = [
        _FakeAttachment("text/plain"),
        _FakeAttachment("image/png"),
        _FakeAttachment("image/jpeg"),
    ]

    result = _find_image_attachment(attachments)

    assert result is attachments[1]


def test_find_image_attachment_returns_none_when_no_images():
    attachments = [_FakeAttachment("text/plain"), _FakeAttachment("application/pdf")]

    assert _find_image_attachment(attachments) is None


def test_find_image_attachment_returns_none_for_empty_list():
    assert _find_image_attachment([]) is None


def test_find_image_attachment_handles_none_content_type():
    attachments = [_FakeAttachment(None)]

    assert _find_image_attachment(attachments) is None


def test_find_image_attachment_rejects_unsupported_image_types():
    # Gemini doesn't accept these, even though they're valid image/* types.
    attachments = [_FakeAttachment("image/gif"), _FakeAttachment("image/svg+xml")]

    assert _find_image_attachment(attachments) is None


def test_find_image_attachment_accepts_mime_type_with_charset_parameter():
    attachments = [_FakeAttachment("image/png; charset=binary")]

    result = _find_image_attachment(attachments)

    assert result is attachments[0]


from discord_bot import _clean_mime_type


def test_clean_mime_type_strips_parameters_and_lowercases():
    assert _clean_mime_type("image/PNG; charset=binary") == "image/png"


def test_clean_mime_type_leaves_plain_type_untouched():
    assert _clean_mime_type("image/jpeg") == "image/jpeg"


import asyncio
from unittest.mock import AsyncMock, Mock, patch

from brain.memory import get_recent_messages, init_db
from tests.conftest import TEST_DATABASE_URL


class _FakeTypingContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeChannel:
    def __init__(self, channel_id, fetchable_message=None, fetch_error=None):
        self.id = channel_id
        self.sent = []
        self._fetchable_message = fetchable_message
        self._fetch_error = fetch_error

    def typing(self):
        return _FakeTypingContext()

    async def send(self, content):
        self.sent.append(content)

    async def fetch_message(self, message_id):
        if self._fetch_error is not None:
            raise self._fetch_error
        return self._fetchable_message


class _FakeAuthor:
    def __init__(self, author_id, is_bot=False):
        self.id = author_id
        self.bot = is_bot


class _FakeMessageReference:
    def __init__(self, message_id=1, resolved=None):
        self.message_id = message_id
        self.resolved = resolved


class _FakeMessage:
    def __init__(
        self,
        content,
        author_id,
        channel_id,
        attachments=None,
        reference=None,
        fetchable_message=None,
        fetch_error=None,
    ):
        self.content = content
        self.author = _FakeAuthor(author_id)
        self.channel = _FakeChannel(
            channel_id, fetchable_message=fetchable_message, fetch_error=fetch_error
        )
        self.attachments = attachments or []
        self.reference = reference


class _FakeReadableAttachment:
    def __init__(self, content_type, size, data=b"fake-bytes"):
        self.content_type = content_type
        self.size = size
        self._data = data

    async def read(self):
        return self._data


def test_on_message_sends_fallback_reply_when_processing_raises():
    async def scenario():
        conn = init_db(TEST_DATABASE_URL)
        gemini_client = Mock()
        gemini_client.generate_with_tools.side_effect = RuntimeError("boom")

        client = discord_bot.JarvisDiscordClient(
            conn=conn, gemini_client=gemini_client, allowed_channel_id=111, owner_id=222
        )
        message = _FakeMessage("hello", author_id=222, channel_id=111)

        await client.on_message(message)

        assert len(message.channel.sent) == 1
        assert "couldn't process" in message.channel.sent[0].lower()

    asyncio.run(scenario())


def test_on_message_replies_normally_when_nothing_fails(conn):
    from brain.gemini_client import ToolReply

    async def scenario(conn):
        gemini_client = Mock()
        gemini_client.generate_with_tools.return_value = ToolReply(
            text="Hello there!", function_name=None, function_args=None
        )

        client = discord_bot.JarvisDiscordClient(
            conn=conn, gemini_client=gemini_client, allowed_channel_id=111, owner_id=222
        )
        message = _FakeMessage("hi", author_id=222, channel_id=111)

        await client.on_message(message)

        assert message.channel.sent == ["Hello there!"]

    asyncio.run(scenario(conn))


def test_on_message_rejects_oversized_image_without_calling_gemini():
    from discord_bot import MAX_IMAGE_SIZE_BYTES

    async def scenario():
        conn = init_db(TEST_DATABASE_URL)
        gemini_client = Mock()

        client = discord_bot.JarvisDiscordClient(
            conn=conn, gemini_client=gemini_client, allowed_channel_id=111, owner_id=222
        )
        oversized = _FakeReadableAttachment(
            "image/png", size=MAX_IMAGE_SIZE_BYTES + 1
        )
        message = _FakeMessage(
            "check this out", author_id=222, channel_id=111, attachments=[oversized]
        )

        await client.on_message(message)

        gemini_client.generate_with_tools.assert_not_called()
        assert len(message.channel.sent) == 1
        assert "too large" in message.channel.sent[0].lower()

    asyncio.run(scenario())


def test_on_message_passes_cleaned_mime_type_for_accepted_image(conn):
    from brain.gemini_client import ToolReply

    async def scenario(conn):
        gemini_client = Mock()
        gemini_client.generate_with_tools.return_value = ToolReply(
            text="nice photo", function_name=None, function_args=None
        )

        client = discord_bot.JarvisDiscordClient(
            conn=conn, gemini_client=gemini_client, allowed_channel_id=111, owner_id=222
        )
        attachment = _FakeReadableAttachment(
            "image/PNG; charset=binary", size=1024, data=b"img-bytes"
        )
        message = _FakeMessage(
            "what is this", author_id=222, channel_id=111, attachments=[attachment]
        )

        await client.on_message(message)

        gemini_client.generate_with_tools.assert_called_once()
        call_kwargs = gemini_client.generate_with_tools.call_args.kwargs
        assert call_kwargs["image_bytes"] == b"img-bytes"
        assert call_kwargs["image_mime_type"] == "image/png"

    asyncio.run(scenario(conn))


def test_on_message_defers_silently_when_cloud_role_and_local_heartbeat_is_fresh():
    from brain.memory import touch_local_heartbeat

    async def scenario():
        conn = init_db(TEST_DATABASE_URL)
        touch_local_heartbeat(conn)
        gemini_client = Mock()

        client = discord_bot.JarvisDiscordClient(
            conn=conn,
            gemini_client=gemini_client,
            allowed_channel_id=111,
            owner_id=222,
            instance_role="cloud",
        )
        message = _FakeMessage("hello", author_id=222, channel_id=111)

        await client.on_message(message)

        assert message.channel.sent == []
        gemini_client.generate_with_tools.assert_not_called()

    asyncio.run(scenario())


def test_on_message_answers_when_cloud_role_and_local_heartbeat_is_stale():
    from brain.gemini_client import ToolReply

    async def scenario():
        conn = init_db(TEST_DATABASE_URL)
        conn.execute("DELETE FROM local_heartbeat")
        conn.commit()
        gemini_client = Mock()
        gemini_client.generate_with_tools.return_value = ToolReply(
            text="Hello there!", function_name=None, function_args=None
        )
        gemini_client.review_memory.return_value = {"profile": [], "lessons": []}
        client = discord_bot.JarvisDiscordClient(
            conn=conn,
            gemini_client=gemini_client,
            allowed_channel_id=111,
            owner_id=222,
            instance_role="cloud",
        )
        message = _FakeMessage("hi", author_id=222, channel_id=111)

        await client.on_message(message)

        assert message.channel.sent == ["Hello there!"]

    asyncio.run(scenario())


def test_on_message_answers_normally_when_local_role_regardless_of_heartbeat():
    from brain.gemini_client import ToolReply

    async def scenario():
        conn = init_db(TEST_DATABASE_URL)
        gemini_client = Mock()
        gemini_client.generate_with_tools.return_value = ToolReply(
            text="Hello there!", function_name=None, function_args=None
        )
        gemini_client.review_memory.return_value = {"profile": [], "lessons": []}
        client = discord_bot.JarvisDiscordClient(
            conn=conn,
            gemini_client=gemini_client,
            allowed_channel_id=111,
            owner_id=222,
            instance_role="local",
        )
        message = _FakeMessage("hi", author_id=222, channel_id=111)

        await client.on_message(message)

        assert message.channel.sent == ["Hello there!"]

    asyncio.run(scenario())


def test_heartbeat_loop_survives_a_failed_touch_and_keeps_running():
    async def scenario():
        conn = init_db(TEST_DATABASE_URL)
        client = discord_bot.JarvisDiscordClient(
            conn=conn,
            gemini_client=Mock(),
            allowed_channel_id=111,
            owner_id=222,
            instance_role="local",
        )
        call_count = 0
        done_event = threading.Event()

        def failing_then_working_touch(_conn):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("simulated transient failure")
            else:
                done_event.set()

        with patch("discord_bot.touch_local_heartbeat", side_effect=failing_then_working_touch):
            with patch("discord_bot.asyncio.sleep", new=AsyncMock()):
                task = asyncio.create_task(client._heartbeat_loop())
                # Patching discord_bot.asyncio.sleep also patches the real,
                # shared asyncio module's sleep, so anything that relies on
                # asyncio.sleep to yield (including a plain sleep(0)) never
                # actually suspends. Wait on a threading.Event instead, via a
                # real executor future, which is unaffected by that patch.
                await asyncio.get_event_loop().run_in_executor(None, done_event.wait, 5)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        assert call_count >= 2

    asyncio.run(scenario())


def test_on_ready_does_not_spawn_a_second_heartbeat_task_when_one_is_already_running():
    async def scenario():
        conn = init_db(TEST_DATABASE_URL)
        client = discord_bot.JarvisDiscordClient(
            conn=conn,
            gemini_client=Mock(),
            allowed_channel_id=111,
            owner_id=222,
            instance_role="local",
        )

        await client.on_ready()
        first_task = client._heartbeat_task
        await client.on_ready()
        second_task = client._heartbeat_task

        assert first_task is second_task

        first_task.cancel()
        try:
            await first_task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_local_role_gets_a_dedicated_heartbeat_connection_distinct_from_chat_conn():
    conn = init_db(TEST_DATABASE_URL)

    client = discord_bot.JarvisDiscordClient(
        conn=conn,
        gemini_client=Mock(),
        allowed_channel_id=111,
        owner_id=222,
        database_url=TEST_DATABASE_URL,
        instance_role="local",
    )

    assert client._heartbeat_conn is not None
    assert client._heartbeat_conn is not conn


def test_on_message_reconnects_the_chat_connection_when_it_was_closed():
    from brain.gemini_client import ToolReply

    async def scenario():
        conn = init_db(TEST_DATABASE_URL)
        conn.close()
        gemini_client = Mock()
        gemini_client.generate_with_tools.return_value = ToolReply(
            text="Hello there!", function_name=None, function_args=None
        )
        gemini_client.review_memory.return_value = {"profile": [], "lessons": []}
        client = discord_bot.JarvisDiscordClient(
            conn=conn,
            gemini_client=gemini_client,
            allowed_channel_id=111,
            owner_id=222,
            database_url=TEST_DATABASE_URL,
            instance_role="local",
        )
        message = _FakeMessage("hi", author_id=222, channel_id=111)

        await client.on_message(message)

        assert message.channel.sent == ["Hello there!"]
        assert not client._conn.closed

    asyncio.run(scenario())


def test_heartbeat_loop_reconnects_its_own_connection_when_it_was_closed():
    async def scenario():
        conn = init_db(TEST_DATABASE_URL)
        client = discord_bot.JarvisDiscordClient(
            conn=conn,
            gemini_client=Mock(),
            allowed_channel_id=111,
            owner_id=222,
            database_url=TEST_DATABASE_URL,
            instance_role="local",
        )
        client._heartbeat_conn.close()
        done_event = threading.Event()

        with patch("discord_bot.touch_local_heartbeat", side_effect=lambda c: done_event.set()):
            with patch("discord_bot.asyncio.sleep", new=AsyncMock()):
                task = asyncio.create_task(client._heartbeat_loop())
                await asyncio.get_event_loop().run_in_executor(None, done_event.wait, 5)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        assert not client._heartbeat_conn.closed

    asyncio.run(scenario())


def test_resolve_replied_to_returns_none_when_no_reference():
    async def scenario():
        message = _FakeMessage("hi", author_id=222, channel_id=111)

        result = await discord_bot._resolve_replied_to(message)

        assert result is None

    asyncio.run(scenario())


def test_resolve_replied_to_uses_cached_resolved_message():
    async def scenario():
        class _Resolved:
            content = "open my trading journal"

        reference = _FakeMessageReference(resolved=_Resolved())
        message = _FakeMessage("do that", author_id=222, channel_id=111, reference=reference)

        result = await discord_bot._resolve_replied_to(message)

        assert result == "open my trading journal"

    asyncio.run(scenario())


def test_resolve_replied_to_fetches_when_not_cached():
    async def scenario():
        class _Fetched:
            content = "open my trading journal"

        reference = _FakeMessageReference(resolved=None)
        message = _FakeMessage(
            "do that",
            author_id=222,
            channel_id=111,
            reference=reference,
            fetchable_message=_Fetched(),
        )

        result = await discord_bot._resolve_replied_to(message)

        assert result == "open my trading journal"

    asyncio.run(scenario())


def test_resolve_replied_to_returns_none_when_fetch_fails():
    async def scenario():
        reference = _FakeMessageReference(resolved=None)
        message = _FakeMessage(
            "do that",
            author_id=222,
            channel_id=111,
            reference=reference,
            fetch_error=RuntimeError("not found"),
        )

        result = await discord_bot._resolve_replied_to(message)

        assert result is None

    asyncio.run(scenario())


def test_on_message_passes_replied_to_context_through_to_run_turn(conn):
    from brain.gemini_client import ToolReply

    async def scenario(conn):
        gemini_client = Mock()
        gemini_client.generate_with_tools.return_value = ToolReply(
            text="Got it.", function_name=None, function_args=None
        )

        class _Resolved:
            content = "open my trading journal"

        reference = _FakeMessageReference(resolved=_Resolved())
        client = discord_bot.JarvisDiscordClient(
            conn=conn, gemini_client=gemini_client, allowed_channel_id=111, owner_id=222
        )
        message = _FakeMessage("do that", author_id=222, channel_id=111, reference=reference)

        await client.on_message(message)

        call_args = gemini_client.generate_with_tools.call_args
        sent_user_message = call_args.args[3]
        assert "open my trading journal" in sent_user_message
        assert "do that" in sent_user_message

    asyncio.run(scenario(conn))


def test_on_message_routes_through_brains_and_appends_tag(conn, monkeypatch):
    from brain.brains import BrainResult
    from brain.conversation import Brains
    import brain.conversation as conversation
    from brain.router import RouteDecision

    monkeypatch.setattr(
        conversation,
        "route",
        lambda message, has_image, history, has_pending, groq_api_key, groq_model, is_reply=False, continues_claude=False:
            RouteDecision("normal", "judge", message, None),
    )

    class _FakeBrain:
        def __init__(self, name, text):
            self.name = name
            self._text = text

        def respond(self, ctx):
            return BrainResult(self._text, self.name)

    brains = Brains(
        gemini=_FakeBrain("gemini", "Hi from gemini."),
        claude=None, groq=None, groq_api_key="k", groq_model="m", show_tag=True,
    )

    async def scenario(conn):
        client = discord_bot.JarvisDiscordClient(
            conn=conn, gemini_client=Mock(), allowed_channel_id=111, owner_id=222,
            brains=brains,
        )
        message = _FakeMessage("hi", author_id=222, channel_id=111)

        await client.on_message(message)

        assert message.channel.sent == ["Hi from gemini.\n— gemini"]

    asyncio.run(scenario(conn))


def test_on_ready_starts_consolidation_loop_only_for_cloud_role():
    async def scenario():
        conn = init_db(TEST_DATABASE_URL)
        client = discord_bot.JarvisDiscordClient(
            conn=conn,
            gemini_client=Mock(),
            allowed_channel_id=111,
            owner_id=222,
            database_url=TEST_DATABASE_URL,
            instance_role="cloud",
        )

        await client.on_ready()

        assert client._consolidation_task is not None
        client._consolidation_task.cancel()
        try:
            await client._consolidation_task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_on_ready_does_not_start_consolidation_loop_for_local_role():
    async def scenario():
        conn = init_db(TEST_DATABASE_URL)
        client = discord_bot.JarvisDiscordClient(
            conn=conn,
            gemini_client=Mock(),
            allowed_channel_id=111,
            owner_id=222,
            instance_role="local",
        )

        await client.on_ready()

        assert client._consolidation_task is None
        if client._heartbeat_task is not None:
            client._heartbeat_task.cancel()
            try:
                await client._heartbeat_task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_consolidation_loop_survives_a_failed_iteration_and_keeps_running():
    async def scenario():
        conn = init_db(TEST_DATABASE_URL)
        client = discord_bot.JarvisDiscordClient(
            conn=conn,
            gemini_client=Mock(),
            allowed_channel_id=111,
            owner_id=222,
            database_url=TEST_DATABASE_URL,
            instance_role="cloud",
        )
        call_count = 0
        done_event = threading.Event()

        def failing_then_working(_conn, _client):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("simulated transient failure")
            done_event.set()
            return 0

        with patch("discord_bot.consolidate_old_messages", side_effect=failing_then_working):
            with patch("discord_bot.asyncio.sleep", new=AsyncMock()):
                task = asyncio.create_task(client._consolidation_loop())
                await asyncio.get_event_loop().run_in_executor(None, done_event.wait, 5)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        assert call_count >= 2

    asyncio.run(scenario())


import re

import discord

from brain.daily_list import (
    ButtonOutcome,
    ButtonSpec,
    ListMessageMissing,
    ListRender,
    PingCard,
    PingMessageMissing,
)
from brain.reminders import ItemNotFoundError


class _FakeInteractionResponse:
    def __init__(self):
        self.deferred = False
        self.messages = []

    async def defer(self, **kwargs):
        self.deferred = True

    async def send_message(self, content, ephemeral=False):
        self.messages.append((content, ephemeral))


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, ephemeral=False):
        self.sent.append((content, ephemeral))


class _FakeInteraction:
    def __init__(self, user_id, message_id=500):
        self.user = _FakeAuthor(user_id)
        self.message = Mock(id=message_id)
        self.response = _FakeInteractionResponse()
        self.followup = _FakeFollowup()
        self.edits = []
        self.deleted = False
        self.delete_fails = False

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)

    async def delete_original_response(self):
        if self.delete_fails:
            raise discord.HTTPException(Mock(status=403), "no permission")
        self.deleted = True


def _client(role="local"):
    return discord_bot.JarvisDiscordClient(
        conn=init_db(TEST_DATABASE_URL), gemini_client=Mock(),
        allowed_channel_id=111, owner_id=222, instance_role=role,
    )


def test_button_template_matches_only_jarvis_ids():
    for good in ("rem:done:5", "rem:s10:12", "rem:s60:1", "list:t3", "list:r44"):
        assert re.fullmatch(discord_bot.BUTTON_TEMPLATE, good)
    for bad in ("rem:nap:5", "list:x3", "list:t", "other"):
        assert not re.fullmatch(discord_bot.BUTTON_TEMPLATE, bad)


def test_reminder_view_has_done_and_two_snoozes():
    async def scenario():
        view = discord_bot.reminder_view(5)
        ids = [child.item.custom_id for child in view.children]
        assert ids == ["rem:done:5", "rem:s10:5", "rem:s60:5"]

    asyncio.run(scenario())


def test_list_view_styles_done_items_and_is_none_when_empty():
    async def scenario():
        assert discord_bot.list_view([]) is None
        view = discord_bot.list_view([
            ButtonSpec("list:t1", "1. Gym", True), ButtonSpec("list:t2", "2. Bank", False),
        ])
        styles = [child.item.style for child in view.children]
        assert styles == [discord.ButtonStyle.success, discord.ButtonStyle.secondary]

    asyncio.run(scenario())


def test_setup_hook_registers_the_dynamic_button():
    async def scenario():
        client = _client()
        client.add_dynamic_items = Mock()
        await client.setup_hook()
        client.add_dynamic_items.assert_called_once_with(discord_bot.JarvisButton)

    asyncio.run(scenario())


def test_handle_button_rejects_other_users():
    async def scenario():
        client = _client()
        interaction = _FakeInteraction(user_id=999)

        await client.handle_button(interaction, "list:t1")

        assert interaction.response.messages == [("These buttons aren't for you.", True)]
        assert interaction.edits == []

    asyncio.run(scenario())


def test_handle_button_defers_to_local_when_cloud_and_local_is_fresh():
    async def scenario():
        client = _client(role="cloud")
        interaction = _FakeInteraction(user_id=222)
        with patch("discord_bot.is_local_heartbeat_fresh", return_value=True), \
                patch("discord_bot.apply_button") as apply:
            await client.handle_button(interaction, "list:t1")
        apply.assert_not_called()
        assert interaction.response.deferred is False

    asyncio.run(scenario())


def test_handle_button_edits_a_reminder_message_with_an_embed_and_removes_buttons():
    async def scenario():
        client = _client()
        interaction = _FakeInteraction(user_id=222)
        card = PingCard("~~Close trades~~", "✅ Done at 21:03 · 📌 Other", 0x4E5058, False)
        outcome = ButtonOutcome("reminder", card=card)
        with patch("discord_bot.apply_button", return_value=outcome):
            await client.handle_button(interaction, "rem:done:5")
        assert interaction.response.deferred is True
        assert len(interaction.edits) == 1
        edit = interaction.edits[0]
        assert edit["content"] == "<@222>"
        assert edit["view"] is None
        assert edit["embed"].title == "~~Close trades~~"
        assert edit["embed"].description == "✅ Done at 21:03 · 📌 Other"
        assert edit["embed"].color.value == 0x4E5058

    asyncio.run(scenario())


def test_handle_button_reminder_click_reattaches_buttons_when_the_card_says_so():
    async def scenario():
        client = _client()
        interaction = _FakeInteraction(user_id=222)
        card = PingCard("Close trades", "💤 Snoozed until 21:13 · 📌 Other", 0x99AAB5, True)
        outcome = ButtonOutcome("reminder", card=card)
        with patch("discord_bot.apply_button", return_value=outcome):
            await client.handle_button(interaction, "rem:s10:5")
        view = interaction.edits[0]["view"]
        assert view is not None
        ids = [child.item.custom_id for child in view.children]
        assert ids == ["rem:done:5", "rem:s10:5", "rem:s60:5"]

    asyncio.run(scenario())


def test_handle_button_passes_the_clicked_messages_id_into_apply_button():
    # Fix for: Done on a stale ping used to mark TODAY done instead of the
    # day the clicked ping actually fired on — apply_button now needs the
    # clicked message id to look that up.
    async def scenario():
        client = _client()
        interaction = _FakeInteraction(user_id=222, message_id=8001)
        card = PingCard("Creatine", "desc", 0x99AAB5, True)
        outcome = ButtonOutcome("reminder", card=card)
        with patch("discord_bot.apply_button", return_value=outcome) as apply:
            await client.handle_button(interaction, "rem:done:5")
        assert apply.call_args.args[-1] == 8001

    asyncio.run(scenario())


def test_handle_button_reminder_click_always_overwrites_content_and_embed_on_an_old_plain_text_message():
    # Backward compatibility regression: a ping posted before this deploy
    # was plain text with no embed. handle_button must not depend on the
    # message's prior shape — it always ends up content=mention + embed +
    # view, upgrading the old message to the new look on the first click.
    async def scenario():
        client = _client()
        interaction = _FakeInteraction(user_id=222)
        interaction.message.embeds = []
        interaction.message.content = "<@222> ⏰ Close trades"
        card = PingCard("~~Close trades~~", "✅ Done at 21:03 · 📌 Other", 0x4E5058, False)
        outcome = ButtonOutcome("reminder", card=card)
        with patch("discord_bot.apply_button", return_value=outcome):
            await client.handle_button(interaction, "rem:done:5")
        edit = interaction.edits[0]
        assert edit["content"] == "<@222>"
        assert edit["embed"].title == "~~Close trades~~"
        assert edit["view"] is None

    asyncio.run(scenario())


def test_handle_button_list_click_always_clears_content_on_an_old_plain_text_message():
    async def scenario():
        client = _client()
        interaction = _FakeInteraction(user_id=222, message_id=777)
        interaction.message.embeds = []
        interaction.message.content = "📋 old plain text list"
        render = ListRender("📋 list", "**1**  Gym", "0 of 1 done", [ButtonSpec("list:t1", "1", False)])
        outcome = ButtonOutcome("list", list_render=render, list_message_id=777)
        with patch("discord_bot.apply_button", return_value=outcome):
            await client.handle_button(interaction, "list:t1")
        edit = interaction.edits[0]
        assert edit["content"] == ""
        assert edit["embed"].title == "📋 list"
        assert edit["embed"].description == "**1**  Gym"

    asyncio.run(scenario())


def test_handle_button_edits_todays_list_message_with_an_embed():
    async def scenario():
        client = _client()
        interaction = _FakeInteraction(user_id=222, message_id=777)
        render = ListRender("📋 list", "**1**  Gym", "1 of 1 done", [ButtonSpec("list:t1", "✓1", True)])
        outcome = ButtonOutcome("list", list_render=render, list_message_id=777)
        with patch("discord_bot.apply_button", return_value=outcome):
            await client.handle_button(interaction, "list:t1")
        edit = interaction.edits[0]
        assert edit["content"] == ""
        assert edit["embed"].title == "📋 list"
        assert edit["embed"].description == "**1**  Gym"
        assert edit["view"] is not None
        ids = [child.item.custom_id for child in edit["view"].children]
        assert ids == ["list:t1"]

    asyncio.run(scenario())


def test_handle_button_does_not_overwrite_a_stale_list_message():
    async def scenario():
        client = _client()
        interaction = _FakeInteraction(user_id=222, message_id=111)  # yesterday's
        render = ListRender("📋 today", "body", "", [])
        outcome = ButtonOutcome("list", list_render=render, list_message_id=777)
        with patch("discord_bot.apply_button", return_value=outcome):
            await client.handle_button(interaction, "list:t1")
        assert interaction.edits == []
        assert interaction.followup.sent == [("Ticked — the pinned list shows the latest.", True)]

    asyncio.run(scenario())


def test_handle_button_reports_a_missing_item():
    async def scenario():
        client = _client()
        interaction = _FakeInteraction(user_id=222)
        with patch("discord_bot.apply_button", side_effect=ItemNotFoundError("t9")):
            await client.handle_button(interaction, "list:t9")
        assert interaction.followup.sent == [("That item no longer exists.", True)]

    asyncio.run(scenario())


def test_handle_button_reports_a_database_failure():
    async def scenario():
        client = _client()
        interaction = _FakeInteraction(user_id=222)
        with patch("discord_bot.apply_button", side_effect=RuntimeError("db down")):
            await client.handle_button(interaction, "list:t1")
        assert interaction.followup.sent == [("Couldn't reach memory — try again.", True)]

    asyncio.run(scenario())


def test_handle_button_reports_memory_unreachable_before_acknowledging():
    async def scenario():
        with patch("discord_bot.init_db", return_value=Mock()):
            client = discord_bot.JarvisDiscordClient(
                conn=init_db(TEST_DATABASE_URL), gemini_client=Mock(),
                allowed_channel_id=111, owner_id=222, instance_role="local",
                database_url="postgresql://unused",
            )
        interaction = _FakeInteraction(user_id=222)
        with patch("discord_bot.ensure_connected", side_effect=RuntimeError("db down")), \
                patch("discord_bot.apply_button") as apply:
            await client.handle_button(interaction, "list:t1")
        assert interaction.response.messages == [("Couldn't reach memory — try again.", True)]
        assert interaction.edits == []
        apply.assert_not_called()

    asyncio.run(scenario())


def test_handle_button_reports_when_the_message_edit_fails():
    async def scenario():
        client = _client()
        interaction = _FakeInteraction(user_id=222)
        interaction.edit_original_response = AsyncMock(
            side_effect=discord.HTTPException(Mock(status=500, reason="boom"), "boom")
        )
        card = PingCard("~~Close trades~~", "✅ Done at 21:03 · 📌 Other", 0x4E5058, False)
        outcome = ButtonOutcome("reminder", card=card)
        with patch("discord_bot.apply_button", return_value=outcome):
            await client.handle_button(interaction, "rem:done:5")
        assert interaction.followup.sent == [
            ("Done — but I couldn't update that message.", True)
        ]

    asyncio.run(scenario())


class _SenderMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.pinned = False

    async def pin(self):
        self.pinned = True


class _SenderPartial:
    def __init__(self, missing=False):
        self.missing = missing
        self.edits = []
        self.unpinned = False

    async def edit(self, **kwargs):
        if self.missing:
            raise discord.NotFound(Mock(status=404, reason="Not Found"), "Unknown Message")
        self.edits.append(kwargs)

    async def unpin(self):
        raise discord.HTTPException(Mock(status=500, reason="boom"), "boom")

    async def delete(self):
        if self.missing:
            raise discord.NotFound(Mock(status=404, reason="Not Found"), "Unknown Message")
        self.deleted = True


class _SenderChannel:
    def __init__(self, partial=None):
        self.sent = []
        self.partial = partial or _SenderPartial()

    async def send(self, content=None, embed=None, view=None):
        self.sent.append((content, embed, view))
        return _SenderMessage(4242)

    def get_partial_message(self, message_id):
        return self.partial


def _sender(channel):
    fake_client = Mock()
    fake_client.get_channel.return_value = channel
    return discord_bot.DiscordSender(fake_client, channel_id=111, owner_id=222)


def test_sender_sends_a_reminder_with_mention_embed_and_buttons():
    async def scenario():
        channel = _SenderChannel()
        message_id = await _sender(channel).send_reminder(5, "Close trades", "trading", "21:00", None)
        content, embed, view = channel.sent[0]
        assert message_id == 4242
        assert content == "<@222>"
        assert embed.title == "Close trades"
        assert embed.description == "`21:00` · 📈 Trading"
        assert embed.color.value == 0xF0B232
        assert view is not None

    asyncio.run(scenario())


def test_sender_sends_a_reminder_with_a_weather_line():
    async def scenario():
        channel = _SenderChannel()
        await _sender(channel).send_reminder(
            5, "Gym", "fitness", "06:00", None,
            weather="🌦️ 14°C (feels 12°C) · rain 40% · wind 18 km/h",
        )
        embed = channel.sent[0][1]
        assert embed.description == (
            "`06:00` · 🏋️ Fitness\n🌦️ 14°C (feels 12°C) · rain 40% · wind 18 km/h"
        )

    asyncio.run(scenario())


def test_sender_marks_late_reminders():
    async def scenario():
        channel = _SenderChannel()
        await _sender(channel).send_reminder(5, "Close trades", "trading", "21:00", "21:00")
        embed = channel.sent[0][1]
        assert embed.description == "`21:00` · 📈 Trading · late — was due 21:00"

    asyncio.run(scenario())


def test_sender_posts_and_pins_the_list():
    async def scenario():
        channel = _SenderChannel()
        render = ListRender("📋 list", "**1**  Gym", "0 of 1 done", [])
        message_id = await _sender(channel).post_list(render)
        assert message_id == 4242
        content, embed, view = channel.sent[0]
        assert content is None
        assert embed.title == "📋 list"
        assert embed.description == "**1**  Gym"
        assert embed.footer.text == "0 of 1 done"

    asyncio.run(scenario())


def test_sender_raises_list_message_missing_when_edit_hits_not_found():
    async def scenario():
        channel = _SenderChannel(partial=_SenderPartial(missing=True))
        render = ListRender("📋 list", "body", "", [])
        with pytest.raises(ListMessageMissing):
            await _sender(channel).edit_list(1, render)

    asyncio.run(scenario())


def test_sender_edit_list_clears_old_plain_text_content():
    async def scenario():
        channel = _SenderChannel()
        render = ListRender("📋 list", "**1**  Gym", "", [])
        await _sender(channel).edit_list(1, render)
        edit = channel.partial.edits[0]
        assert edit["content"] == ""
        assert edit["embed"].title == "📋 list"

    asyncio.run(scenario())


def test_sender_edit_ping_attaches_buttons_only_when_the_card_says_so():
    async def scenario():
        channel = _SenderChannel()
        active = PingCard("Creatine", "`15:00` · 💊 Supplements", 0x23A55A, True)
        await _sender(channel).edit_ping(1, 5, active)
        edit = channel.partial.edits[0]
        assert edit["view"] is not None

        done = PingCard("~~Creatine~~", "✅ Done at 15:06 · 💊 Supplements", 0x4E5058, False)
        await _sender(channel).edit_ping(1, 5, done)
        assert channel.partial.edits[1]["view"] is None

    asyncio.run(scenario())


def test_sender_edit_ping_raises_ping_message_missing_when_edit_hits_not_found():
    async def scenario():
        channel = _SenderChannel(partial=_SenderPartial(missing=True))
        card = PingCard("Creatine", "desc", 0x23A55A, True)
        with pytest.raises(PingMessageMissing):
            await _sender(channel).edit_ping(1, 5, card)

    asyncio.run(scenario())


def test_sender_unpin_swallows_discord_errors():
    async def scenario():
        await _sender(_SenderChannel()).unpin(1)  # must not raise

    asyncio.run(scenario())


def test_on_ready_spawns_the_scheduler_only_for_cloud():
    async def scenario():
        cloud = _client(role="cloud")
        local = _client(role="local")
        with patch.object(cloud, "_scheduler_loop", new=AsyncMock()), \
                patch.object(cloud, "_consolidation_loop", new=AsyncMock()), \
                patch.object(local, "_heartbeat_loop", new=AsyncMock()):
            await cloud.on_ready()
            await local.on_ready()
            assert cloud._scheduler_task is not None
            assert local._scheduler_task is None
            cloud._scheduler_task.cancel()

    asyncio.run(scenario())


def test_scheduler_loop_survives_a_failed_tick_and_keeps_running():
    async def scenario():
        client = _client(role="cloud")
        call_count = 0
        done_event = threading.Event()

        async def failing_then_working_tick(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("simulated failure")
            done_event.set()

        # A bare AsyncMock() never actually suspends the task (it has no
        # real await inside it), so patching asyncio.sleep with one turns
        # the loop into a tight, non-yielding busy loop: the task never
        # cedes control back to the event loop, so the run_in_executor
        # future below can never get scheduled and the test hangs forever.
        # Delegate to the real sleep(0) so each iteration still yields.
        real_sleep = asyncio.sleep

        async def fast_sleep(*args, **kwargs):
            await real_sleep(0)

        with patch("discord_bot.run_scheduler_tick", side_effect=failing_then_working_tick):
            with patch("discord_bot.asyncio.sleep", side_effect=fast_sleep):
                task = asyncio.create_task(client._scheduler_loop())
                await asyncio.get_event_loop().run_in_executor(None, done_event.wait, 5)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        assert call_count >= 2

    asyncio.run(scenario())


def test_sender_sends_a_reping_with_still_to_do_title_and_numbering():
    async def scenario():
        channel = _SenderChannel()
        await _sender(channel).send_reminder(
            5, "Trading and journal", "trading", "17:30", None, ping_number=2, ping_total=4,
        )
        content, embed, view = channel.sent[0]
        assert content == "<@222>"
        assert embed.title == "Still to do: Trading and journal"
        assert embed.description == "`17:30` · 📈 Trading · reminder 2 of 4"
        assert embed.color.value == 0xF0B232
        assert view is not None

    asyncio.run(scenario())


def test_sender_sends_a_nudge_with_mention_and_embed_without_buttons():
    async def scenario():
        channel = _SenderChannel()
        message_id = await _sender(channel).send_nudge(["• Duolingo", "• Video"])
        content, embed, view = channel.sent[0]
        assert message_id == 4242
        assert content == "<@222>"
        assert embed.title == "🔔 Still on your list today"
        assert embed.description == "• Duolingo\n• Video"
        assert view is None

    asyncio.run(scenario())


def test_sender_delete_ping_deletes_the_message():
    async def scenario():
        channel = _SenderChannel()
        await _sender(channel).delete_ping(7)
        assert channel.partial.deleted is True

    asyncio.run(scenario())


def test_sender_delete_ping_ignores_an_already_deleted_message():
    async def scenario():
        channel = _SenderChannel(partial=_SenderPartial(missing=True))
        await _sender(channel).delete_ping(7)  # must not raise

    asyncio.run(scenario())


# ---- approval cards (computer access) ----

from brain.approvals import ApprovalRequest, PendingApprovals


class _CardMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _CardChannel:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self.message = _CardMessage()

    async def send(self, content=None, embed=None, view=None, file=None):
        if self.fail:
            raise discord.HTTPException(Mock(status=500, reason="boom"), "boom")
        self.sent.append({"content": content, "embed": embed, "view": view, "file": file})
        return self.message


def _broker(channel, pending=None):
    fake_client = Mock()
    fake_client.get_channel.return_value = channel
    pending = pending or PendingApprovals()
    return discord_bot.DiscordApprovalBroker(fake_client, 111, 222, pending), pending


def _req(**overrides):
    base = dict(machine="pc", summary="run a command", detail="dir C:\\", reason="list files")
    base.update(overrides)
    return ApprovalRequest(**base)


async def _wait_for_card(channel):
    for _ in range(200):
        if channel.sent:
            return channel.sent[0]
        await asyncio.sleep(0.005)
    raise AssertionError("no card was posted")


def _card_ids(card):
    return [child.item.custom_id for child in card["view"].children]


def _approval_client(pending, role="local"):
    client = _client(role=role)
    client.approvals = pending
    return client


def test_button_template_matches_approval_ids():
    good_id = "a" * 32
    for good in (f"appr:{good_id}:allow", f"appr:{good_id}:deny"):
        assert re.fullmatch(discord_bot.BUTTON_TEMPLATE, good)
    for bad in (f"appr:{good_id}:maybe", "appr:xyz:allow", "appr::allow"):
        assert not re.fullmatch(discord_bot.BUTTON_TEMPLATE, bad)


def test_approval_card_shows_title_machine_reason_detail_and_buttons():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        card = await _wait_for_card(channel)
        embed = card["embed"]
        assert embed.title == "🔐 Claude wants to run a command"
        fields = {f.name: f.value for f in embed.fields}
        assert fields["Machine"] == "PC"
        assert fields["Why"] == "list files"
        assert "dir C:\\" in fields["Command"]
        assert card["file"] is None
        assert card["content"] == "<@222>"
        labels = [child.item.label for child in card["view"].children]
        assert labels == ["Allow", "Deny"]
        approval_id = _card_ids(card)[0].split(":")[1]
        pending.resolve(approval_id, True, "Dad")
        await task

    asyncio.run(scenario())


def test_server_machine_label_and_missing_reason_omits_why():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        task = asyncio.create_task(broker.request(_req(machine="server", reason=""), timeout_s=5))
        card = await _wait_for_card(channel)
        fields = {f.name: f.value for f in card["embed"].fields}
        assert fields["Machine"] == "Server"
        assert "Why" not in fields
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_batch_card_labels_allow_all_n():
    async def scenario():
        channel = _CardChannel()
        broker, _ = _broker(channel)
        task = asyncio.create_task(broker.request(_req(batch_size=4), timeout_s=5))
        card = await _wait_for_card(channel)
        labels = [child.item.label for child in card["view"].children]
        assert labels == ["Allow all 4", "Deny"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_long_detail_is_attached_as_details_txt():
    async def scenario():
        channel = _CardChannel()
        broker, _ = _broker(channel)
        detail = "x" * 901
        task = asyncio.create_task(broker.request(_req(detail=detail), timeout_s=5))
        card = await _wait_for_card(channel)
        assert card["file"].filename == "details.txt"
        assert card["file"].fp.read().decode() == detail
        fields = {f.name: f.value for f in card["embed"].fields}
        assert detail not in fields["Command"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_detail_of_exactly_900_chars_stays_inline():
    async def scenario():
        channel = _CardChannel()
        broker, _ = _broker(channel)
        task = asyncio.create_task(broker.request(_req(detail="y" * 900), timeout_s=5))
        card = await _wait_for_card(channel)
        assert card["file"] is None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_owner_allow_resolves_and_edits_the_card():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        client = _approval_client(pending)
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        card = await _wait_for_card(channel)
        interaction = _FakeInteraction(user_id=222)
        await client.handle_button(interaction, _card_ids(card)[0])
        result = await task
        assert (result.allowed, result.outcome) == (True, "allowed")
        assert result.approver
        assert interaction.response.deferred is True
        assert "Allowed" in interaction.edits[0]["embed"].title
        assert interaction.edits[0]["view"] is None

    asyncio.run(scenario())


def test_owner_deny_resolves_and_edits_the_card():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        client = _approval_client(pending)
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        card = await _wait_for_card(channel)
        interaction = _FakeInteraction(user_id=222)
        await client.handle_button(interaction, _card_ids(card)[1])
        result = await task
        assert (result.allowed, result.outcome) == (False, "denied")
        assert "Denied" in interaction.edits[0]["embed"].title

    asyncio.run(scenario())


def test_timeout_denies_and_edits_the_card():
    async def scenario():
        channel = _CardChannel()
        broker, _ = _broker(channel)
        result = await broker.request(_req(), timeout_s=0.05)
        assert (result.allowed, result.outcome, result.approver) == (False, "timeout", None)
        edit = channel.message.edits[0]
        assert edit["embed"].title == "⏱ Timed out — denied"
        assert edit["view"] is None

    asyncio.run(scenario())


def test_late_click_after_timeout_has_no_effect():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        client = _approval_client(pending)
        result = await broker.request(_req(), timeout_s=0.05)
        assert result.outcome == "timeout"
        interaction = _FakeInteraction(user_id=222)
        await client.handle_button(interaction, _card_ids(channel.sent[0])[0])
        assert interaction.response.messages == [("This request already ended.", True)]
        assert interaction.edits == []

    asyncio.run(scenario())


def test_non_owner_click_gets_not_yours_and_does_not_resolve():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        client = _approval_client(pending)
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        card = await _wait_for_card(channel)
        interaction = _FakeInteraction(user_id=999)
        await client.handle_button(interaction, _card_ids(card)[0])
        assert interaction.response.messages == [("This request isn't yours to answer.", True)]
        assert not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_unknown_id_is_ignored_silently_by_an_instance_that_did_not_create_it():
    async def scenario():
        client = _approval_client(PendingApprovals())
        interaction = _FakeInteraction(user_id=222)
        await client.handle_button(interaction, "appr:" + "b" * 32 + ":allow")
        assert interaction.response.messages == []
        assert interaction.response.deferred is False
        stranger = _FakeInteraction(user_id=999)
        await client.handle_button(stranger, "appr:" + "b" * 32 + ":allow")
        assert stranger.response.messages == []

    asyncio.run(scenario())


def test_cloud_instance_handles_its_own_approval_even_when_local_is_fresh():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        client = _approval_client(pending, role="cloud")
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        card = await _wait_for_card(channel)
        interaction = _FakeInteraction(user_id=222)
        with patch("discord_bot.is_local_heartbeat_fresh", return_value=True):
            await client.handle_button(interaction, _card_ids(card)[0])
        assert (await task).outcome == "allowed"

    asyncio.run(scenario())


def test_only_the_creating_instance_resolves_the_approval():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        other = _approval_client(PendingApprovals())
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        card = await _wait_for_card(channel)
        interaction = _FakeInteraction(user_id=222)
        await other.handle_button(interaction, _card_ids(card)[0])
        assert interaction.response.messages == []
        assert not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_send_failure_returns_error_outcome_which_counts_as_deny():
    async def scenario():
        broker, pending = _broker(_CardChannel(fail=True))
        result = await broker.request(_req(), timeout_s=5)
        assert (result.allowed, result.outcome, result.approver) == (False, "error", None)

    asyncio.run(scenario())


def test_client_owns_a_broker_and_registry():
    async def scenario():
        client = _client()
        assert isinstance(client.approvals, PendingApprovals)
        assert isinstance(client.approval_broker, discord_bot.DiscordApprovalBroker)

    asyncio.run(scenario())


# ---- approval broker hardening ----


def test_double_click_first_wins_and_second_gets_already_ended():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        client = _approval_client(pending)
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        card = await _wait_for_card(channel)
        first, second = _FakeInteraction(user_id=222), _FakeInteraction(user_id=222)
        await client.handle_button(first, _card_ids(card)[0])
        await client.handle_button(second, _card_ids(card)[0])
        assert (await task).outcome == "allowed"
        assert second.response.messages == [("This request already ended.", True)]
        assert second.edits == []

    asyncio.run(scenario())


def test_allow_then_deny_first_wins():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        client = _approval_client(pending)
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        card = await _wait_for_card(channel)
        allow, deny = _FakeInteraction(user_id=222), _FakeInteraction(user_id=222)
        await client.handle_button(allow, _card_ids(card)[0])
        await client.handle_button(deny, _card_ids(card)[1])
        result = await task
        assert (result.allowed, result.outcome) == (True, "allowed")
        assert deny.response.messages == [("This request already ended.", True)]

    asyncio.run(scenario())


def test_approver_falls_back_to_user_id_without_display_name():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        client = _approval_client(pending)
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        card = await _wait_for_card(channel)
        interaction = _FakeInteraction(user_id=222)
        assert not hasattr(interaction.user, "display_name")
        await client.handle_button(interaction, _card_ids(card)[0])
        assert (await task).approver == "222"

    asyncio.run(scenario())


def test_display_name_is_used_when_present():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        client = _approval_client(pending)
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        card = await _wait_for_card(channel)
        interaction = _FakeInteraction(user_id=222)
        interaction.user.display_name = "Dad"
        await client.handle_button(interaction, _card_ids(card)[0])
        assert (await task).approver == "Dad"
        assert interaction.edits[0]["embed"].title == "✅ Allowed by Dad"

    asyncio.run(scenario())


def test_allow_decision_stands_when_the_card_edit_fails():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        client = _approval_client(pending)
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        card = await _wait_for_card(channel)
        interaction = _FakeInteraction(user_id=222)
        interaction.edit_original_response = AsyncMock(side_effect=RuntimeError("boom"))
        await client.handle_button(interaction, _card_ids(card)[0])
        assert (await task).outcome == "allowed"

    asyncio.run(scenario())


def test_allow_decision_stands_when_defer_fails():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        client = _approval_client(pending)
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        card = await _wait_for_card(channel)
        interaction = _FakeInteraction(user_id=222)
        interaction.response.defer = AsyncMock(side_effect=RuntimeError("boom"))
        await client.handle_button(interaction, _card_ids(card)[0])
        assert (await task).outcome == "allowed"

    asyncio.run(scenario())


def test_timeout_card_edit_failure_still_returns_timeout():
    async def scenario():
        channel = _CardChannel()
        channel.message.edit = AsyncMock(side_effect=RuntimeError("boom"))
        broker, pending = _broker(channel)
        result = await broker.request(_req(), timeout_s=0.05)
        assert result.outcome == "timeout"

    asyncio.run(scenario())


def test_cancel_while_waiting_edits_card_to_cancelled_and_discards():
    async def scenario():
        channel = _CardChannel()
        broker, pending = _broker(channel)
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        card = await _wait_for_card(channel)
        approval_id = _card_ids(card)[0].split(":")[1]
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert channel.message.edits[0]["embed"].title == "Cancelled"
        assert channel.message.edits[0]["view"] is None
        assert pending.resolve(approval_id, True, "Dad") is False

    asyncio.run(scenario())


def test_cancel_while_waiting_survives_a_failing_cancel_edit():
    async def scenario():
        channel = _CardChannel()
        channel.message.edit = AsyncMock(side_effect=RuntimeError("boom"))
        broker, pending = _broker(channel)
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        await _wait_for_card(channel)
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)

    asyncio.run(scenario())


def test_cancel_during_send_discards_and_reraises():
    async def scenario():
        started = asyncio.Event()

        class _HangingChannel:
            async def send(self, **kwargs):
                started.set()
                await asyncio.sleep(60)

        async def send(content=None, **kwargs):
            await _HangingChannel().send()

        channel = Mock()
        channel.send = send
        broker, pending = _broker(channel)
        before = set(pending._open)
        task = asyncio.create_task(broker.request(_req(), timeout_s=5))
        for _ in range(200):
            if pending._open:
                break
            await asyncio.sleep(0.005)
        approval_id = next(iter(set(pending._open) - before))
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert approval_id not in pending._open
        assert pending.resolve(approval_id, True, "Dad") is False

    asyncio.run(scenario())


# ---- computer access wiring (Task 5) ----

from datetime import datetime, timedelta, timezone

from brain import computer_state as cs
from brain.brains.claude import ClaudeBrain
from brain.computer_tools import ComputerTaskState
from brain.conversation import Brains


class _FilesChannel(_FakeChannel):
    """A channel that also records file sends."""

    def __init__(self, channel_id):
        super().__init__(channel_id)
        self.file_sends = []

    async def send(self, content=None, files=None, view=None):
        if files is not None:
            self.file_sends.append([f.filename for f in files])
            return
        self.sent.append(content)


class _TextSender:
    """Stands in for DiscordSender in queue/bypass tests."""

    def __init__(self, *args):
        self.texts = []
        self.files = []

    async def send_text(self, text):
        self.texts.append(text)

    async def send_attachments(self, attachments):
        self.files.append([n for n, _ in attachments])


def _claude_brains():
    return Brains(
        gemini=Mock(), claude=ClaudeBrain("tok", "sonnet", cli_path="claude"), groq=None,
        groq_api_key=None, groq_model="m",
    )


def _computer_client(role="local", brains=None):
    return discord_bot.JarvisDiscordClient(
        conn=init_db(TEST_DATABASE_URL), gemini_client=Mock(), allowed_channel_id=111,
        owner_id=222, database_url=TEST_DATABASE_URL, instance_role=role,
        brains=brains or _claude_brains(),
    )


def test_computer_mode_is_pc_on_local_and_server_on_cloud():
    for role, machine in (("local", "pc"), ("cloud", "server")):
        client = _computer_client(role)
        brains = client._brains
        assert brains.computer_machine == machine
        brain = brains.computer_claude_factory()
        assert brain is not brains.claude and brain.computer.machine == machine
        assert (brain.computer.queue_task is not None) == (machine == "server")
        assert brain.computer.max_steps == 60 and brain.computer.timeout_s == 1800
        assert brain.computer.state is not None and brain.oauth_token == "tok"


def test_no_computer_mode_without_a_claude_brain_or_database():
    plain = Brains(gemini=Mock(), claude=None, groq=None, groq_api_key=None, groq_model="m")
    client = _computer_client(brains=plain)
    assert client._computer_enabled is False and plain.computer_claude_factory is None
    no_db = Brains(gemini=Mock(), claude=ClaudeBrain("t"), groq=None, groq_api_key=None,
                   groq_model="m")
    discord_bot.JarvisDiscordClient(
        conn=init_db(TEST_DATABASE_URL), gemini_client=Mock(), allowed_channel_id=111,
        owner_id=222, brains=no_db,
    )
    assert no_db.computer_claude_factory is None


def test_each_task_gets_its_own_registered_state_on_the_discord_loop():
    async def scenario():
        client = _computer_client()
        seen = []

        def fake_run_turn(conn, gemini, text, brains=None, attachments_out=None, **kw):
            seen.append((brains.computer_claude_factory(), brains.computer_claude_factory(),
                         dict(client._tasks)))
            return "done"

        with patch("discord_bot.run_turn", side_effect=fake_run_turn):
            reply, files = await client._run_chat_turn("tidy up")

        first, second, registered = seen[0]
        assert reply == "done" and files == []
        assert first.computer.state is not second.computer.state
        assert first.computer.state.task_id != second.computer.state.task_id
        assert set(registered) == {first.computer.state.task_id, second.computer.state.task_id}
        assert first.computer.discord_loop is asyncio.get_running_loop()
        assert client._tasks == {}  # closed at the end of the turn

    asyncio.run(scenario())


def test_progress_line_posts_edits_in_place_and_closes_with_the_stop_button_removed():
    async def scenario():
        channel = _CardChannel()
        client = Mock()
        client.get_channel.return_value = channel
        state = ComputerTaskState(task_id="a" * 12)
        line = discord_bot.ProgressLine(discord_bot.DiscordSender(client, 111, 222), state)

        await line.update("⏳ Working… 1 steps done")
        await line.update("⏳ Working… 2 steps done")
        assert channel.sent == []  # short turns show no progress line yet
        await line.update("⏳ Working… 3 steps done")
        await line.update("⏳ Working… 4 steps done")
        assert len(channel.sent) == 1
        view = channel.sent[0]["view"]
        assert [c.item.custom_id for c in view.children] == ["stop:" + "a" * 12]
        assert channel.message.edits[0]["content"] == "⏳ Working… 4 steps done"
        assert channel.message.edits[0]["view"] is not None

        state.steps = 4
        await line.finish()
        await line.update("late")  # ignored once finished
        assert channel.message.edits[-1] == {"content": "✅ Finished (4 steps)", "view": None}
        assert len(channel.message.edits) == 2

    asyncio.run(scenario())


def test_stop_button_sets_that_tasks_stopped_flag_for_the_owner_only():
    async def scenario():
        client = _computer_client()
        state = ComputerTaskState(task_id="b" * 12)
        other = ComputerTaskState(task_id="c" * 12)
        client._tasks = {
            state.task_id: discord_bot.ProgressLine(Mock(), state),
            other.task_id: discord_bot.ProgressLine(Mock(), other),
        }
        stranger = _FakeInteraction(user_id=999)
        await client.handle_button(stranger, "stop:" + "b" * 12)
        assert state.stopped is False
        assert stranger.response.messages == [("This task isn't yours to stop.", True)]

        pending_card = asyncio.get_running_loop().create_future()
        state.track(pending_card)
        owner = _FakeInteraction(user_id=222)
        await client.handle_button(owner, "stop:" + "b" * 12)
        assert state.stopped is True and other.stopped is False
        assert pending_card.cancelled()  # Stop also withdraws a waiting approval
        assert owner.edits[0]["view"] is None

        quiet = _FakeInteraction(user_id=222)
        await client.handle_button(quiet, "stop:" + "d" * 12)  # unknown: another instance's task
        assert quiet.response.messages == [] and quiet.edits == []  # unknown id: silent

        # not the owner: told so only for a task this instance knows; unknown: silent
        stranger = _FakeInteraction(user_id=999)
        await client.handle_button(stranger, "stop:" + "d" * 12)
        cloud = _computer_client("cloud")
        cloud_click = _FakeInteraction(user_id=222)
        await cloud.handle_button(cloud_click, "stop:" + "d" * 12)
        assert stranger.response.messages == [] and cloud_click.response.messages == []

    asyncio.run(scenario())


def test_button_template_matches_stop_ids():
    assert re.fullmatch(discord_bot.BUTTON_TEMPLATE, "stop:" + "a" * 12)
    for bad in ("stop:", "stop:xyz", "stop:" + "g" * 12):
        assert not re.fullmatch(discord_bot.BUTTON_TEMPLATE, bad)


def test_attachments_are_sent_after_the_reply():
    async def scenario():
        client = _computer_client()
        message = _FakeMessage("edit it", author_id=222, channel_id=111)
        message.channel = _FilesChannel(111)

        def fake_run_turn(conn, gemini, text, brains=None, attachments_out=None, **kw):
            attachments_out.extend([("output.txt", b"x"), ("changes.diff", b"y")])
            return "All done."

        with patch("discord_bot.run_turn", side_effect=fake_run_turn):
            await client.on_message(message)

        assert message.channel.sent == ["All done."]
        assert message.channel.file_sends == [["output.txt", "changes.diff"]]

    asyncio.run(scenario())


def test_more_than_ten_attachments_are_split_across_messages():
    async def scenario():
        channel = _FilesChannel(111)
        await discord_bot.send_attachments(channel, [(f"f{i}.txt", b"x") for i in range(12)])
        assert [len(b) for b in channel.file_sends] == [10, 2]

    asyncio.run(scenario())


def test_a_non_owner_cannot_use_chat_commands(conn):
    async def scenario():
        client = _computer_client()
        client._conn = conn
        message = _FakeMessage("/bypass", author_id=999, channel_id=111)
        await client.on_message(message)
        assert message.channel.sent == []
        assert cs.get_bypass_until(conn, datetime.now(timezone.utc)) is None

    asyncio.run(scenario())


def test_owner_chat_command_runs_without_a_brain(conn):
    async def scenario():
        client = _computer_client()
        client._conn = conn
        message = _FakeMessage("/safe", author_id=222, channel_id=111)
        await client.on_message(message)
        assert message.channel.sent == ["🛡️ Bypass off — I'll ask again before risky steps."]

    asyncio.run(scenario())


# -- PC task queue pickup on the local instance --

NOW_UTC = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def test_queue_pickup_fails_stale_runs_drops_expired_then_runs_oldest_first(conn):
    async def scenario():
        client = _computer_client()
        now = datetime.now(timezone.utc)
        stale = cs.queue_pc_task(conn, "crashed one", now - timedelta(hours=3))
        cs.next_pc_task(conn, now)  # claims it -> running
        old = cs.queue_pc_task(conn, "too old", now - timedelta(days=8))
        second = cs.queue_pc_task(conn, "second", now - timedelta(minutes=5))
        first = cs.queue_pc_task(conn, "first", now - timedelta(minutes=50))
        sender = _TextSender()
        ran = []

        async def fake_turn(text, **kwargs):
            ran.append((text, {k: v for k, v in kwargs.items() if k != "outcome_out"}))
            kwargs["outcome_out"].update(ok=True, brain="sonnet")
            return f"result of {text}", ([("output.txt", b"x")] if text == "first" else [])

        with patch("discord_bot.DiscordSender", return_value=sender), \
                patch.object(client, "_run_chat_turn", side_effect=fake_turn):
            await client._process_pc_queue()

        status = dict(conn.execute("SELECT id, status FROM pc_tasks").fetchall())
        assert status == {stale: "failed", old: "expired", first: "done", second: "done"}
        assert [r[0] for r in ran] == ["first", "second"]
        # queued text never runs chat commands and always goes to Claude
        assert all(k == {"allow_commands": False, "tier_override": "claude_only", "queued_task": True} for _, k in ran)
        assert sender.texts[0].startswith("🗑️ Dropped queued PC task #%d" % old)
        assert "too old" in sender.texts[0]
        assert sender.texts[1:] == [
            "▶ Starting queued task: first", "result of first",
            "▶ Starting queued task: second", "result of second",
        ]
        assert sender.files == [["output.txt"]]
        note = conn.execute("SELECT note FROM pc_tasks WHERE id = %s", (stale,)).fetchone()[0]
        assert note == "interrupted (restart)"

    asyncio.run(scenario())


def test_a_failing_queued_task_is_recorded_failed_and_the_queue_goes_on(conn):
    async def scenario():
        client = _computer_client()
        now = datetime.now(timezone.utc)
        bad = cs.queue_pc_task(conn, "boom", now - timedelta(minutes=9))
        good = cs.queue_pc_task(conn, "fine", now - timedelta(minutes=1))
        sender = _TextSender()

        async def fake_turn(text, **kwargs):
            if text == "boom":
                raise RuntimeError("it broke")
            kwargs["outcome_out"].update(ok=True, brain="sonnet")
            return "ok", []

        with patch("discord_bot.DiscordSender", return_value=sender), \
                patch.object(client, "_run_chat_turn", side_effect=fake_turn):
            await client._process_pc_queue()

        status = dict(conn.execute("SELECT id, status FROM pc_tasks").fetchall())
        assert status == {bad: "failed", good: "done"}
        assert any("failed: it broke" in t for t in sender.texts)

    asyncio.run(scenario())


def test_on_ready_starts_the_queue_pickup_once_and_only_on_local():
    async def scenario():
        local, cloud = _computer_client("local"), _computer_client("cloud")
        runs = []

        async def fake_process():
            runs.append(1)

        with patch.object(local, "_process_pc_queue", side_effect=fake_process), \
                patch.object(local, "_heartbeat_loop", new=AsyncMock()), \
                patch.object(cloud, "_scheduler_loop", new=AsyncMock()), \
                patch.object(cloud, "_consolidation_loop", new=AsyncMock()), \
                patch.object(cloud, "_process_pc_queue", side_effect=fake_process):
            await local.on_ready()
            await local.on_ready()  # a reconnect must not fail a genuinely running task
            await cloud.on_ready()
            for _ in range(5):
                await asyncio.sleep(0)
            local._queue_task.cancel()
            await asyncio.gather(local._queue_task, return_exceptions=True)
        assert runs == [1] and cloud._queue_task is None

    asyncio.run(scenario())


def test_cloud_queue_tool_stores_the_task_and_says_the_pc_is_off(conn):
    async def scenario():
        client = _computer_client("cloud")
        text = await client._queue_for_pc("zip my reports")
        task = cs.list_pc_tasks(conn)[0]
        assert task["text"] == "zip my reports" and task["requested_at"].tzinfo is not None
        assert "Your PC is off — I'll do this when it's back on." in text

    asyncio.run(scenario())


# -- bypass warnings and activity pruning in the scheduler --


def test_bypass_warning_is_posted_once_fifteen_minutes_before_expiry(conn):
    async def scenario():
        sender = _TextSender()
        cs.set_bypass(conn, NOW_UTC + timedelta(hours=2))
        await discord_bot.run_bypass_tick(conn, sender, NOW_UTC)
        assert sender.texts == []  # too early

        later = NOW_UTC + timedelta(hours=2) - timedelta(minutes=14)
        await discord_bot.run_bypass_tick(conn, sender, later)
        await discord_bot.run_bypass_tick(conn, sender, later + timedelta(seconds=30))
        assert len(sender.texts) == 1 and "ends in 14 min" in sender.texts[0]

    asyncio.run(scenario())


def test_bypass_ended_is_posted_once_at_expiry(conn):
    async def scenario():
        sender = _TextSender()
        cs.set_bypass(conn, NOW_UTC + timedelta(minutes=10))
        end = NOW_UTC + timedelta(minutes=10)
        await discord_bot.run_bypass_tick(conn, sender, NOW_UTC)
        await discord_bot.run_bypass_tick(conn, sender, end)
        await discord_bot.run_bypass_tick(conn, sender, end + timedelta(seconds=30))
        await discord_bot.run_bypass_tick(conn, sender, end + timedelta(hours=1))
        assert [t for t in sender.texts if t.startswith("🛡️ Bypass ended")] == ["🛡️ Bypass ended"]
        assert len(sender.texts) == 2  # the one warning + the end

    asyncio.run(scenario())


def test_no_messages_when_bypass_was_never_on_or_ended_with_safe(conn):
    async def scenario():
        sender = _TextSender()
        await discord_bot.run_bypass_tick(conn, sender, NOW_UTC)
        cs.set_bypass(conn, NOW_UTC + timedelta(hours=1))
        cs.clear_bypass(conn)
        await discord_bot.run_bypass_tick(conn, sender, NOW_UTC + timedelta(hours=2))
        assert sender.texts == []

    asyncio.run(scenario())


def test_activity_is_pruned_once_per_day(conn):
    async def scenario():
        client = _computer_client("cloud")
        client._scheduler_conn = conn
        with patch("discord_bot.prune_activity") as prune:
            await client._prune_activity_if_due(NOW_UTC)
            await client._prune_activity_if_due(NOW_UTC + timedelta(hours=3))
            await client._prune_activity_if_due(NOW_UTC + timedelta(days=1))
        assert prune.call_count == 2

    asyncio.run(scenario())


def test_scheduler_loop_runs_the_bypass_tick_and_prune():
    async def scenario():
        client = _computer_client("cloud")
        done = threading.Event()

        async def fake_bypass(*args, **kwargs):
            done.set()

        real_sleep = asyncio.sleep

        async def fast_sleep(*a, **k):
            await real_sleep(0)

        with patch("discord_bot.run_scheduler_tick", new=AsyncMock()), \
                patch("discord_bot.run_bypass_tick", side_effect=fake_bypass), \
                patch.object(client, "_prune_activity_if_due", new=AsyncMock()) as prune, \
                patch("discord_bot.asyncio.sleep", side_effect=fast_sleep):
            task = asyncio.create_task(client._scheduler_loop())
            await asyncio.get_event_loop().run_in_executor(None, done.wait, 5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert prune.await_count >= 1

    asyncio.run(scenario())


# ---- review fixes: queued tasks run tainted on Claude only ----


def _queued_run(conn, outcome, reply="the reply"):
    """Runs one queued task whose turn reports `outcome`; returns (status, note, texts)."""
    async def scenario():
        client = _computer_client()
        task = cs.queue_pc_task(conn, "do it", datetime.now(timezone.utc) - timedelta(minutes=1))
        sender = _TextSender()

        async def fake_turn(text, **kwargs):
            kwargs["outcome_out"].update(outcome)
            return reply, []

        with patch("discord_bot.DiscordSender", return_value=sender), \
                patch.object(client, "_run_chat_turn", side_effect=fake_turn):
            await client._process_pc_queue()
        row = conn.execute("SELECT status, note FROM pc_tasks WHERE id = %s", (task,)).fetchone()
        return row[0], row[1], sender.texts

    return asyncio.run(scenario())


def test_queued_task_is_done_only_when_claude_answered_cleanly(conn):
    status, note, texts = _queued_run(conn, {"ok": True, "brain": "sonnet"})
    assert status == "done" and texts[-1] == "the reply"


def test_queued_task_fails_when_no_brain_answered(conn):
    from brain.conversation import BUSY_MESSAGE
    status, note, texts = _queued_run(conn, {"ok": False, "brain": None}, reply=BUSY_MESSAGE)
    assert status == "failed" and note == BUSY_MESSAGE
    assert texts[-1] == "⚠️ Queued task failed: " + BUSY_MESSAGE


def test_queued_task_fails_when_a_different_brain_answered(conn):
    status, note, texts = _queued_run(conn, {"ok": True, "brain": "gemini"})
    assert status == "failed" and texts[-1].startswith("⚠️ Queued task failed:")


def test_queued_task_brain_is_tainted_and_normal_chat_is_not():
    async def scenario():
        client = _computer_client()
        base = client._brains.claude
        states = []

        async def fake_turn(text, **kwargs):
            states.append(client._new_computer_brain(base, "pc").computer.state.tainted)
            kwargs["outcome_out"].update(ok=True, brain="sonnet")
            return "ok", []

        sender = _TextSender()
        conn = init_db(TEST_DATABASE_URL)
        conn.execute("TRUNCATE pc_tasks RESTART IDENTITY")
        cs.queue_pc_task(conn, "x", datetime.now(timezone.utc))
        with patch("discord_bot.DiscordSender", return_value=sender), \
                patch.object(client, "_run_chat_turn", side_effect=fake_turn):
            await client._process_pc_queue()
        states.append(client._new_computer_brain(base, "pc").computer.state.tainted)
        assert states == [True, False]

    asyncio.run(scenario())


def test_batches_respect_the_byte_budget_as_well_as_the_file_count():
    big = 10 * 1024 * 1024
    files = [("a", b"x" * big), ("b", b"x" * big), ("c", b"x" * big), ("d", b"y")]
    batches = discord_bot._batch_attachments(files)
    assert [[n for n, _ in b] for b in batches] == [["a", "b"], ["c", "d"]]
    assert [[n for n, _ in b] for b in discord_bot._batch_attachments(
        [("huge", b"x" * (30 * 1024 * 1024)), ("s", b"x")])] == [["huge"], ["s"]]


def test_two_bypass_ticks_race_to_one_warning(conn):
    async def scenario():
        sender = _TextSender()
        cs.set_bypass(conn, NOW_UTC + timedelta(minutes=10))
        await asyncio.gather(
            discord_bot.run_bypass_tick(conn, sender, NOW_UTC),
            discord_bot.run_bypass_tick(init_db(TEST_DATABASE_URL), sender, NOW_UTC),
        )
        assert len(sender.texts) == 1

    asyncio.run(scenario())


# ---- final review fixes ----


def test_progress_line_appears_after_five_seconds_even_with_few_steps():
    async def scenario():
        channel = _CardChannel()
        client = Mock()
        client.get_channel.return_value = channel
        line = discord_bot.ProgressLine(
            discord_bot.DiscordSender(client, 111, 222), ComputerTaskState(task_id="e" * 12)
        )
        await line.update("one")
        assert channel.sent == []
        line._started -= discord_bot.PROGRESS_MIN_SECONDS + 1
        await line.update("two")
        assert len(channel.sent) == 1

    asyncio.run(scenario())


def test_a_short_turn_posts_no_progress_line_and_no_finished_message():
    async def scenario():
        channel = _CardChannel()
        client = Mock()
        client.get_channel.return_value = channel
        line = discord_bot.ProgressLine(
            discord_bot.DiscordSender(client, 111, 222), ComputerTaskState(task_id="f" * 12)
        )
        await line.update("⏳ Working… 1 steps done")
        await line.finish()
        assert channel.sent == [] and channel.message.edits == []

    asyncio.run(scenario())


def test_commands_do_not_wait_for_the_task_lock(conn):
    async def scenario():
        client = _computer_client()
        message = _FakeMessage("/safe", author_id=222, channel_id=111)
        cs.set_bypass(conn, datetime.now(timezone.utc) + timedelta(hours=2))
        async with client._lock:  # a running task holds the lock
            await asyncio.wait_for(client.on_message(message), timeout=10)
            assert cs.get_bypass_until(conn, datetime.now(timezone.utc)) is None
        assert message.channel.sent == ["🛡️ Bypass off — I'll ask again before risky steps."]
        roles = [(m["role"], m["content"]) for m in get_recent_messages(conn)]
        assert roles[0] == ("user", "/safe") and roles[1][0] == "assistant"

    asyncio.run(scenario())


def test_commands_on_cloud_defer_to_a_fresh_local_heartbeat(conn):
    async def scenario():
        client = _computer_client("cloud")
        cs.set_bypass(conn, datetime.now(timezone.utc) + timedelta(hours=2))
        message = _FakeMessage("/safe", author_id=222, channel_id=111)
        with patch("discord_bot.is_local_heartbeat_fresh", return_value=True):
            await client.on_message(message)
        assert message.channel.sent == []
        assert cs.get_bypass_until(conn, datetime.now(timezone.utc)) is not None

    asyncio.run(scenario())


def test_a_malformed_command_falls_through_to_the_normal_turn(conn):
    async def scenario():
        client = _computer_client()
        message = _FakeMessage("/bypass please", author_id=222, channel_id=111)
        with patch("discord_bot.run_turn", return_value="ok then") as turn:
            await client.on_message(message)
        assert turn.call_count == 1 and message.channel.sent == ["ok then"]

    asyncio.run(scenario())


def test_queue_poll_picks_up_tasks_queued_after_startup_but_not_while_the_lock_is_held(conn):
    async def scenario():
        client = _computer_client()
        sender = _TextSender()
        ran = []

        async def fake_turn(text, **kwargs):
            ran.append(text)
            kwargs["outcome_out"].update(ok=True, brain="sonnet")
            return "ok", []

        with patch("discord_bot.DiscordSender", return_value=sender), \
                patch.object(client, "_run_chat_turn", side_effect=fake_turn):
            await client._process_pc_queue()  # startup pass: nothing queued
            assert ran == []
            cs.queue_pc_task(conn, "later task", datetime.now(timezone.utc))
            async with client._lock:  # a chat turn is running: skip this poll
                await client._process_pc_queue()
            assert ran == []
            await client._process_pc_queue()  # lock free: picked up exactly once
            await client._process_pc_queue()
        assert ran == ["later task"]

    asyncio.run(scenario())


def test_stale_running_tasks_are_failed_only_on_the_first_pass(conn):
    async def scenario():
        client = _computer_client()
        now = datetime.now(timezone.utc)
        cs.queue_pc_task(conn, "was running", now - timedelta(hours=1))
        cs.next_pc_task(conn, now)
        with patch("discord_bot.DiscordSender", return_value=_TextSender()):
            await client._process_pc_queue()
            running = cs.queue_pc_task(conn, "running now", now)
            conn.execute("UPDATE pc_tasks SET status = 'running' WHERE id = %s", (running,))
            await client._process_pc_queue()
        statuses = [r[0] for r in conn.execute("SELECT status FROM pc_tasks ORDER BY id")]
        assert statuses == ["failed", "running"]

    asyncio.run(scenario())


def test_poll_loop_repeats_the_pass_every_interval():
    async def scenario():
        client = _computer_client()
        passes = []

        async def fake_process():
            passes.append(1)
            if len(passes) == 3:
                raise asyncio.CancelledError

        real_sleep = asyncio.sleep

        async def fast_sleep(delay):
            assert delay == discord_bot.QUEUE_POLL_SECONDS == 30
            await real_sleep(0)

        with patch.object(client, "_process_pc_queue", side_effect=fake_process), \
                patch("discord_bot.asyncio.sleep", side_effect=fast_sleep):
            await asyncio.gather(client._pc_queue_runner(), return_exceptions=True)
        assert len(passes) == 3

    asyncio.run(scenario())


def test_per_step_connections_are_plain_autocommit_connections():
    with patch("discord_bot.psycopg.connect") as connect:
        discord_bot._connect("postgresql://x/y")
    connect.assert_called_once_with("postgresql://x/y", autocommit=True)
    client = _computer_client()
    brain = client._brains.computer_claude_factory()
    conn = brain.computer.conn_factory()
    try:
        assert conn.autocommit is True
    finally:
        conn.close()


def test_a_chat_turn_after_a_tainted_one_starts_tainted_for_exactly_one_turn():
    async def scenario():
        client = _computer_client()
        base = client._brains.claude
        seen = []
        script = iter([
            {"taint": True, "tools": []},     # "summarize this page": the task got tainted
            {"taint": False, "tools": []},    # "do it": inherited the taint
            {"taint": False, "tools": []},    # the turn after: clean again
            {"taint": False, "tools": ["web_search"]},  # Jarvis web info tool ran (no task taint)
            {"taint": False, "tools": []},
        ])

        def fake_run_turn(conn, gemini, text, brains=None, attachments_out=None,
                          outcome_out=None, **kw):
            step = next(script)
            brain = brains.computer_claude_factory()
            seen.append(brain.computer.state.tainted)
            if step["taint"]:
                brain.computer.state.tainted = True
                brain.computer.state.self_tainted = True
            outcome_out.update(ok=True, brain="sonnet", tools=step["tools"])
            return "ok"

        with patch("discord_bot.run_turn", side_effect=fake_run_turn):
            for _ in range(5):
                await client._run_chat_turn("x")
        assert seen == [False, True, False, False, True]

    asyncio.run(scenario())


def test_an_inherited_turn_that_reads_untrusted_content_again_keeps_the_carry():
    async def scenario():
        client = _computer_client()
        seen = []
        script = iter([True, True, False, False])  # turn 2 inherits AND re-taints itself

        def fake_run_turn(conn, gemini, text, brains=None, attachments_out=None,
                          outcome_out=None, **kw):
            brain = brains.computer_claude_factory()
            seen.append(brain.computer.state.tainted)
            if next(script):
                brain.computer.state.tainted = True
                brain.computer.state.self_tainted = True
            outcome_out.update(ok=True, brain="sonnet", tools=[])
            return "ok"

        with patch("discord_bot.run_turn", side_effect=fake_run_turn):
            for _ in range(4):
                await client._run_chat_turn("x")
        assert seen == [False, True, True, False]

    asyncio.run(scenario())


def test_a_queued_run_does_not_consume_or_set_the_chat_carry_taint(conn):
    async def scenario():
        client = _computer_client()
        client._carry_taint = True

        def fake_run_turn(conn_, gemini, text, brains=None, attachments_out=None,
                          outcome_out=None, **kw):
            brains.computer_claude_factory().computer.state.tainted = True
            outcome_out.update(ok=True, brain="sonnet", tools=[])
            return "ok"

        client._tainted_turn = True
        with patch("discord_bot.run_turn", side_effect=fake_run_turn):
            await client._run_chat_turn("queued", queued_task=True)
        client._tainted_turn = False
        assert client._carry_taint is True

    asyncio.run(scenario())


def test_queued_task_hitting_a_limit_is_recorded_partial(conn):
    async def scenario():
        client = _computer_client()
        task = cs.queue_pc_task(conn, "big job", datetime.now(timezone.utc) - timedelta(minutes=1))
        sender = _TextSender()

        async def fake_turn(text, **kwargs):
            kwargs["outcome_out"].update(ok=True, brain="sonnet")
            client._last_limit_hit = "steps"
            return "Stopped at the limit (60 steps / 30 min).", []

        with patch("discord_bot.DiscordSender", return_value=sender), \
                patch.object(client, "_run_chat_turn", side_effect=fake_turn):
            await client._process_pc_queue()
        row = conn.execute("SELECT status, note FROM pc_tasks WHERE id = %s", (task,)).fetchone()
        assert row[0] == "partial" and "steps" in row[1]
        assert "Stopped at the limit" in sender.texts[-1]

    asyncio.run(scenario())


def test_queued_task_stopped_by_the_owner_is_recorded_cancelled(conn):
    async def scenario():
        client = _computer_client()
        task = cs.queue_pc_task(conn, "big job", datetime.now(timezone.utc) - timedelta(minutes=1))
        sender = _TextSender()

        async def fake_turn(text, **kwargs):
            kwargs["outcome_out"].update(ok=True, brain="sonnet")
            client._last_task_stopped = True
            return "🛑 Stopped by you after 3 steps.", []

        with patch("discord_bot.DiscordSender", return_value=sender), \
                patch.object(client, "_run_chat_turn", side_effect=fake_turn):
            await client._process_pc_queue()
        row = conn.execute("SELECT status FROM pc_tasks WHERE id = %s", (task,)).fetchone()
        assert row[0] == "cancelled"

    asyncio.run(scenario())


def test_finish_tasks_records_that_the_owner_stopped_the_turn():
    async def scenario():
        client = _computer_client()
        state = ComputerTaskState(task_id="8" * 12)
        state.stop()
        client._tasks = {state.task_id: discord_bot.ProgressLine(Mock(), state)}
        await client._finish_tasks()
        assert client._last_task_stopped is True

    asyncio.run(scenario())


def test_finish_tasks_records_the_limit_and_taint_of_the_turn():
    async def scenario():
        client = _computer_client()
        state = ComputerTaskState(task_id="9" * 12, tainted=True, self_tainted=True)
        state.limit_hit = "time"
        client._tasks = {state.task_id: discord_bot.ProgressLine(Mock(), state)}
        await client._finish_tasks()
        assert client._last_limit_hit == "time" and client._last_task_tainted is True

    asyncio.run(scenario())


def test_done_button_deletes_the_ping_message():
    async def scenario():
        client = _client()
        interaction = _FakeInteraction(user_id=222)
        card = PingCard("~~Close trades~~", "✅ Done at 21:03 · 📌 Other", 0x4E5058, False)
        with patch("discord_bot.apply_button",
                   return_value=ButtonOutcome("reminder", card=card, delete_message=True)):
            await client.handle_button(interaction, "rem:done:5")
        assert interaction.deleted is True and interaction.edits == []

    asyncio.run(scenario())


def test_done_button_falls_back_to_the_done_card_when_deleting_fails():
    async def scenario():
        client = _client()
        interaction = _FakeInteraction(user_id=222)
        interaction.delete_fails = True
        card = PingCard("~~Close trades~~", "✅ Done at 21:03 · 📌 Other", 0x4E5058, False)
        with patch("discord_bot.apply_button",
                   return_value=ButtonOutcome("reminder", card=card, delete_message=True)):
            await client.handle_button(interaction, "rem:done:5")
        assert interaction.edits[0]["embed"].description == "✅ Done at 21:03 · 📌 Other"

    asyncio.run(scenario())
