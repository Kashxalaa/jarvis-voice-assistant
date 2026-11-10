import pytest

from brain.profile import (
    PROFILE_MAX_CHARS, ProfileEditError, apply_edits, format_note, get_profile, set_profile, undo_last,
)

DOC = "# About Alex\n\n## Typical week\n- Weekdays: boxing 06:00\n- Daily: Duolingo\n\n## People\n- Sam: my friend.\n"


def test_replace_changes_only_the_matching_line():
    new, summaries, skipped = apply_edits(DOC, [
        {"section": "Typical week", "action": "replace", "old": "boxing 06:00", "new": "Weekdays: boxing 07:00"}])
    assert "- Weekdays: boxing 07:00" in new and "06:00" not in new
    assert summaries == ["Weekdays: boxing 06:00 → Weekdays: boxing 07:00"] and skipped == []


def test_add_appends_to_the_section():
    new, summaries, _ = apply_edits(DOC, [{"section": "people", "action": "add", "new": "Nika: my brother."}])
    assert new.index("- Nika: my brother.") > new.index("- Sam: my friend.")
    assert summaries == ["+ Nika: my brother."]


def test_add_inside_a_middle_section_goes_before_the_blank_line():
    new, _, _ = apply_edits(DOC, [{"section": "Typical week", "action": "add", "new": "Sunday: rest"}])
    assert "- Daily: Duolingo\n- Sunday: rest\n\n## People" in new


def test_add_to_a_missing_section_creates_it_at_the_end():
    new, _, _ = apply_edits(DOC, [{"section": "Goals", "action": "add", "new": "Run a marathon"}])
    assert new.rstrip().endswith("## Goals\n- Run a marathon")


def test_remove_deletes_the_line():
    new, summaries, _ = apply_edits(DOC, [{"section": "Typical week", "action": "remove", "old": "duolingo"}])
    assert "Duolingo" not in new and summaries == ["− Daily: Duolingo"]


def test_ambiguous_old_text_is_skipped_not_guessed():
    new, summaries, skipped = apply_edits(DOC, [
        {"section": "Typical week", "action": "remove", "old": "a"}])  # matches both lines
    assert new == DOC and summaries == [] and "more than one line" in skipped[0]


def test_missing_section_or_line_is_skipped():
    _, _, skipped = apply_edits(DOC, [
        {"section": "Nope", "action": "remove", "old": "x"},
        {"section": "People", "action": "replace", "old": "nobody", "new": "y"},
        {"section": "People", "action": "explode"}])
    assert len(skipped) == 3


def test_non_list_edits_change_nothing():
    assert apply_edits(DOC, None) == (DOC, [], [])


def test_edits_that_make_it_too_long_are_rejected():
    with pytest.raises(ProfileEditError):
        apply_edits(DOC, [{"section": "People", "action": "add", "new": "x" * PROFILE_MAX_CHARS}])


def test_format_note():
    assert format_note([]) == ""
    assert format_note(["a", "b"]) == "📝 Profile: a; b"
    assert format_note(["a", "b", "c", "d", "e"]) == "📝 Profile: a; b; c; +2 more"


def test_storage_round_trip_history_and_undo(conn):
    assert get_profile(conn) == "" and undo_last(conn) is None
    set_profile(conn, "v1", "seed", "seed")
    set_profile(conn, "v2", "x → y", "tool")
    assert get_profile(conn) == "v2"
    assert undo_last(conn) == "x → y" and get_profile(conn) == "v1"
    assert undo_last(conn) is not None and get_profile(conn) == "v2"   # undo of undo = redo


def test_history_keeps_only_the_last_50(conn):
    for i in range(60):
        set_profile(conn, f"v{i}", "s", "tool")
    assert conn.execute("SELECT count(*) FROM profile_history").fetchone()[0] == 50


def test_section_written_with_its_hashes_matches_the_existing_section():
    new, summaries, skipped = apply_edits(DOC, [
        {"section": "## People", "action": "add", "new": "Nika"},
        {"section": "## Typical week", "action": "remove", "old": "duolingo"}])
    assert new.count("## People") == 1 and "## ## " not in new
    assert "- Nika" in new and "Duolingo" not in new and skipped == []


def test_line_breaks_in_new_text_cannot_create_headings():
    new, _, _ = apply_edits(DOC, [{"section": "People", "action": "add", "new": "Nino\n## Hacked\n- injected"}])
    assert [line for line in new.split("\n") if line.startswith("## ")] == ["## Typical week", "## People"]
    assert "- Nino ## Hacked - injected" in new


def test_adding_a_line_that_is_already_there_is_skipped():
    new, summaries, skipped = apply_edits(DOC, [{"section": "People", "action": "add", "new": "sam: my friend."}])
    assert new == DOC and summaries == [] and "already" in skipped[0]


def test_long_changes_are_shortened_in_the_note():
    long_old = "Trading: ICT methodology, multi-timeframe analysis, Fair Value Gaps, SMT divergence, TradingView"
    long_new = long_old + ", Discord, layout on two monitors"
    note = format_note([f"{long_old} → {long_new}"])
    assert len(note) <= len("📝 Profile: ") + 80
    assert note.endswith("…")


def test_lesson_notes_are_shortened_too():
    from brain.lessons import format_lesson_note

    note = format_lesson_note(["x" * 300])
    assert len(note) <= len("🧠 Lesson: ") + 80 and note.endswith("…")
