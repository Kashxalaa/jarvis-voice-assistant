from unittest.mock import Mock

import pytest


def _fake_gemini_client(embedding=None, summarize_return=None):
    client = Mock()
    client.embed_text.return_value = embedding if embedding is not None else [0.1] * 768
    if summarize_return is not None:
        client.summarize_for_notes.return_value = summarize_return
    return client


def test_create_note_embeds_and_stores(notes_conn):
    from brain.notes import create_note

    client = _fake_gemini_client(embedding=[0.5] * 768)

    note_id = create_note(
        notes_conn, client, "Trading setup", "Configured the journal automation.",
        "trading", 7, source="explicit",
    )

    assert isinstance(note_id, int)
    client.embed_text.assert_called_once()


def test_create_note_raises_note_creation_error_when_embedding_fails(notes_conn):
    from brain.gemini_client import GeminiUnavailableError
    from brain.notes import NoteCreationError, create_note

    client = Mock()
    client.embed_text.side_effect = GeminiUnavailableError("down")

    with pytest.raises(NoteCreationError):
        create_note(notes_conn, client, "T", "C", "tags", 5, source="explicit")


def test_search_notes_returns_matching_note_and_bumps_usage(notes_conn):
    from brain.notes import create_note, search_notes

    embedding = [1.0] + [0.0] * 767
    client = _fake_gemini_client(embedding=embedding)
    create_note(notes_conn, client, "Trading setup", "Journal automation.", "trading", 7)

    results = search_notes(notes_conn, client, "what did we set up for trading?")

    assert len(results) == 1
    assert results[0]["title"] == "Trading setup"
    assert results[0]["use_count"] == 1


def test_search_notes_returns_empty_list_when_no_notes_exist(notes_conn):
    from brain.notes import search_notes

    client = _fake_gemini_client()

    results = search_notes(notes_conn, client, "anything")

    assert results == []


def test_search_notes_returns_empty_list_when_embedding_fails(notes_conn):
    from brain.gemini_client import GeminiUnavailableError
    from brain.notes import search_notes

    client = Mock()
    client.embed_text.side_effect = GeminiUnavailableError("down")

    results = search_notes(notes_conn, client, "anything")

    assert results == []


def test_search_notes_returns_empty_list_when_notes_table_unavailable(conn):
    # `conn` (not `notes_conn`) is the local Postgres fixture, which has no
    # pgvector — a real reproduction of "notes table doesn't exist",
    # not a mocked stand-in for it.
    from brain.notes import search_notes

    client = Mock()
    client.embed_text.return_value = [0.1] * 768

    results = search_notes(conn, client, "anything")

    assert results == []


def test_create_note_raises_note_creation_error_when_notes_table_unavailable(conn):
    from brain.notes import NoteCreationError, create_note

    client = Mock()
    client.embed_text.return_value = [0.1] * 768

    with pytest.raises(NoteCreationError):
        create_note(conn, client, "T", "C", "tags", 5, source="explicit")


def test_consolidate_old_messages_creates_notes_and_advances_pointer(notes_conn):
    from brain.memory import add_message, get_consolidation_state
    from brain.notes import consolidate_old_messages

    for i in range(25):
        add_message(notes_conn, "user", f"message {i}")
    client = _fake_gemini_client(
        embedding=[0.2] * 768,
        summarize_return=[
            {"title": "Early chat", "content": "Talked about setup.", "tags": "setup", "importance": 4}
        ],
    )

    created = consolidate_old_messages(notes_conn, client)

    assert created == 1
    client.summarize_for_notes.assert_called_once()
    state = get_consolidation_state(notes_conn)
    assert state["last_consolidated_message_id"] > 0


def test_consolidate_old_messages_processes_multiple_batches_in_one_run(notes_conn):
    from brain.memory import add_message, get_consolidation_state
    from brain.notes import consolidate_old_messages

    # keep_recent=20 + 3 batches of up to 50 needs well over 70 messages.
    for i in range(130):
        add_message(notes_conn, "user", f"message {i}")
    client = _fake_gemini_client(
        embedding=[0.2] * 768,
        summarize_return=[
            {"title": "Batch note", "content": "Content.", "tags": "", "importance": 4}
        ],
    )

    created = consolidate_old_messages(notes_conn, client)

    assert client.summarize_for_notes.call_count > 1
    assert created == client.summarize_for_notes.call_count
    state = get_consolidation_state(notes_conn)
    # Nothing left to consolidate after this run: id 110 is the boundary
    # (130 - keep_recent(20) = 110 messages eligible, ids 1..110).
    assert state["last_consolidated_message_id"] == 110


def test_consolidate_old_messages_is_a_noop_when_nothing_old_enough(notes_conn):
    from brain.memory import add_message
    from brain.notes import consolidate_old_messages

    for i in range(5):
        add_message(notes_conn, "user", f"message {i}")
    client = _fake_gemini_client()

    created = consolidate_old_messages(notes_conn, client)

    assert created == 0
    client.summarize_for_notes.assert_not_called()


def test_consolidate_old_messages_advances_pointer_even_when_nothing_worth_keeping(notes_conn):
    from brain.memory import add_message, get_consolidation_state
    from brain.notes import consolidate_old_messages

    for i in range(25):
        add_message(notes_conn, "user", f"message {i}")
    client = _fake_gemini_client(summarize_return=[])

    created = consolidate_old_messages(notes_conn, client)

    assert created == 0
    state = get_consolidation_state(notes_conn)
    assert state["last_consolidated_message_id"] > 0


def test_consolidate_old_messages_does_not_advance_pointer_when_summarization_fails(notes_conn):
    from brain.memory import add_message, get_consolidation_state
    from brain.notes import consolidate_old_messages

    for i in range(25):
        add_message(notes_conn, "user", f"message {i}")
    client = _fake_gemini_client()
    client.summarize_for_notes.return_value = None  # Gemini call failed

    created = consolidate_old_messages(notes_conn, client)

    assert created == 0
    state = get_consolidation_state(notes_conn)
    assert state["last_consolidated_message_id"] == 0


def test_consolidate_old_messages_does_not_advance_pointer_when_a_note_fails_to_save(notes_conn):
    from brain.gemini_client import GeminiUnavailableError
    from brain.memory import add_message, get_consolidation_state
    from brain.notes import consolidate_old_messages

    for i in range(25):
        add_message(notes_conn, "user", f"message {i}")
    client = Mock()
    client.embed_text.side_effect = GeminiUnavailableError("down")
    client.summarize_for_notes.return_value = [
        {"title": "A", "content": "B", "tags": "", "importance": 4}
    ]

    created = consolidate_old_messages(notes_conn, client)

    assert created == 0
    state = get_consolidation_state(notes_conn)
    assert state["last_consolidated_message_id"] == 0


def test_consolidate_old_messages_does_not_reprocess_an_already_consolidated_batch(notes_conn):
    from brain.memory import add_message
    from brain.notes import consolidate_old_messages

    for i in range(25):
        add_message(notes_conn, "user", f"message {i}")
    client = _fake_gemini_client(
        embedding=[0.3] * 768,
        summarize_return=[
            {"title": "Batch one", "content": "First run.", "tags": "", "importance": 4}
        ],
    )

    first_run_created = consolidate_old_messages(notes_conn, client)
    # Simulates a second loop iteration after a process restart, before any
    # new messages have arrived — must not reprocess the same batch.
    second_run_created = consolidate_old_messages(notes_conn, client)

    assert first_run_created == 1
    assert second_run_created == 0
    client.summarize_for_notes.assert_called_once()


def test_search_memory_tool_has_the_expected_name():
    from brain.notes import SEARCH_MEMORY_TOOL

    assert SEARCH_MEMORY_TOOL["name"] == "search_memory"


def test_create_note_tool_has_the_expected_name():
    from brain.notes import CREATE_NOTE_TOOL

    assert CREATE_NOTE_TOOL["name"] == "create_note"
