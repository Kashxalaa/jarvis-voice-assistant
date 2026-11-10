import json
from datetime import datetime, timedelta, timezone

import pytest

from brain.memory import init_db, get_action, upsert_action, get_pending_proposal
import brain.automation as automation
from brain.automation import ActionNotFoundError, execute_action


def test_execute_action_raises_when_name_not_found(conn):
    with pytest.raises(ActionNotFoundError):
        execute_action(conn, "nonexistent")


def test_execute_action_opens_a_simple_app_action(conn, monkeypatch):
    upsert_action(conn, "vscode", "app", "C:/apps/code.exe")
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    summary = execute_action(conn, "vscode")

    assert opened == [("app", "C:/apps/code.exe")]
    assert "vscode" in summary


def test_execute_action_opens_every_step_of_a_routine(conn, monkeypatch):
    upsert_action(conn, "journal_folder", "folder", "E:/journal")
    upsert_action(conn, "broker_site", "url", "https://broker.example.com")
    upsert_action(
        conn, "trading_work", "routine",
        json.dumps([{"name": "journal_folder"}, {"name": "broker_site"}]),
    )
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    summary = execute_action(conn, "trading_work")

    assert opened == [
        ("folder", "E:/journal"),
        ("url", "https://broker.example.com"),
    ]
    assert "journal_folder" in summary
    assert "broker_site" in summary


def test_execute_action_skips_missing_routine_steps_without_crashing(conn, monkeypatch):
    upsert_action(conn, "journal_folder", "folder", "E:/journal")
    upsert_action(
        conn, "trading_work", "routine",
        json.dumps([{"name": "journal_folder"}, {"name": "ghost_step"}]),
    )
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    summary = execute_action(conn, "trading_work")

    assert opened == [("folder", "E:/journal")]
    assert "journal_folder" in summary


from brain.automation import ActionExecutionError


def test_execute_action_raises_action_execution_error_when_open_fails(conn, monkeypatch):
    upsert_action(conn, "vscode", "app", "C:/apps/code.exe")

    def failing_open(kind, value):
        raise OSError("file not found")

    monkeypatch.setattr(automation, "_open", failing_open)

    with pytest.raises(ActionExecutionError):
        execute_action(conn, "vscode")


from brain.automation import confirm_pending_action, is_pending_proposal_expired, propose_action


def test_propose_action_saves_a_pending_proposal(conn):
    propose_action(conn, "url", "quickbrew_site", "https://quickbrew.com", "seems like a website")

    proposal = get_pending_proposal(conn)
    assert proposal["kind"] == "url"
    assert proposal["suggested_name"] == "quickbrew_site"
    assert proposal["suggested_value"] == "https://quickbrew.com"
    assert proposal["turns_remaining"] == 3


def test_confirm_pending_action_executes_and_saves_when_confirmed(conn, monkeypatch):
    propose_action(conn, "url", "quickbrew_site", "https://quickbrew.com", "reason")
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    executed_name = confirm_pending_action(conn, confirmed=True)

    assert executed_name == "quickbrew_site"
    assert opened == [("url", "https://quickbrew.com")]
    assert get_action(conn, "quickbrew_site") == {
        "name": "quickbrew_site", "kind": "url", "value": "https://quickbrew.com"
    }
    assert get_pending_proposal(conn) is None


def test_confirm_pending_action_uses_corrected_value_when_given(conn, monkeypatch):
    propose_action(conn, "url", "quickbrew_site", "https://quickbrew.com", "reason")
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    confirm_pending_action(conn, confirmed=True, corrected_value="https://quickbrew.net")

    assert opened == [("url", "https://quickbrew.net")]
    assert get_action(conn, "quickbrew_site")["value"] == "https://quickbrew.net"


def test_confirm_pending_action_clears_without_executing_when_denied(conn, monkeypatch):
    propose_action(conn, "url", "quickbrew_site", "https://quickbrew.com", "reason")
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    executed_name = confirm_pending_action(conn, confirmed=False)

    assert executed_name is None
    assert opened == []
    assert get_action(conn, "quickbrew_site") is None
    assert get_pending_proposal(conn) is None


def test_confirm_pending_action_returns_none_when_nothing_pending(conn):
    assert confirm_pending_action(conn, confirmed=True) is None


def test_is_pending_proposal_expired_true_when_turns_exhausted():
    proposal = {"turns_remaining": 0, "created_at": datetime.now(timezone.utc).isoformat()}
    assert is_pending_proposal_expired(proposal, datetime.now(timezone.utc)) is True


def test_is_pending_proposal_expired_true_when_too_old():
    old_time = datetime.now(timezone.utc) - timedelta(hours=25)
    proposal = {"turns_remaining": 3, "created_at": old_time.isoformat()}
    assert is_pending_proposal_expired(proposal, datetime.now(timezone.utc)) is True


def test_is_pending_proposal_expired_false_when_fresh():
    proposal = {"turns_remaining": 3, "created_at": datetime.now(timezone.utc).isoformat()}
    assert is_pending_proposal_expired(proposal, datetime.now(timezone.utc)) is False


from brain.automation import (
    AUTOMATION_TOOLS,
    CONFIRM_PENDING_ACTION_TOOL,
    PROPOSE_NEW_ACTION_TOOL,
    RUN_AD_HOC_PLACEMENT_TOOL,
    RUN_KNOWN_ACTION_TOOL,
    RUN_ONE_OFF_ACTION_TOOL,
)


def test_automation_tools_have_the_expected_names():
    assert RUN_KNOWN_ACTION_TOOL["name"] == "run_known_action"
    assert PROPOSE_NEW_ACTION_TOOL["name"] == "propose_new_action"
    assert CONFIRM_PENDING_ACTION_TOOL["name"] == "confirm_pending_action"
    assert RUN_AD_HOC_PLACEMENT_TOOL["name"] == "run_ad_hoc_placement"
    assert RUN_ONE_OFF_ACTION_TOOL["name"] == "run_one_off_action"
    assert AUTOMATION_TOOLS == [
        RUN_KNOWN_ACTION_TOOL,
        PROPOSE_NEW_ACTION_TOOL,
        RUN_AD_HOC_PLACEMENT_TOOL,
        RUN_ONE_OFF_ACTION_TOOL,
    ]


def test_confirm_pending_action_raises_and_does_not_save_when_open_fails(conn, monkeypatch):
    from brain.automation import ActionExecutionError

    propose_action(conn, "folder", "ghost_folder", "E:/does/not/exist", "reason")

    def failing_open(kind, value):
        raise OSError("path not found")

    monkeypatch.setattr(automation, "_open", failing_open)

    with pytest.raises(ActionExecutionError):
        confirm_pending_action(conn, confirmed=True)

    assert get_action(conn, "ghost_folder") is None
    assert get_pending_proposal(conn) is None


def test_confirm_pending_action_raises_action_execution_error_for_invalid_kind(conn, monkeypatch):
    from brain.automation import ActionExecutionError

    propose_action(conn, "bogus_kind", "weird", "some-value", "reason")

    with pytest.raises(ActionExecutionError):
        confirm_pending_action(conn, confirmed=True)

    assert get_action(conn, "weird") is None


def test_confirm_pending_action_raises_for_malformed_routine_json(conn):
    from brain.automation import ActionExecutionError

    propose_action(conn, "routine", "broken_routine", "not valid json", "reason")

    with pytest.raises(ActionExecutionError):
        confirm_pending_action(conn, confirmed=True)

    assert get_action(conn, "broken_routine") is None
    assert get_pending_proposal(conn) is None


def test_confirm_pending_action_raises_when_routine_references_missing_action(conn):
    from brain.automation import ActionExecutionError

    propose_action(
        conn, "routine", "trading_work", json.dumps([{"name": "ghost_step"}]), "reason"
    )

    with pytest.raises(ActionExecutionError) as exc_info:
        confirm_pending_action(conn, confirmed=True)

    assert "ghost_step" in str(exc_info.value)
    assert get_action(conn, "trading_work") is None


def test_confirm_pending_action_saves_and_executes_a_valid_new_routine(conn, monkeypatch):
    upsert_action(conn, "journal_folder", "folder", "E:/journal")
    upsert_action(conn, "broker_site", "url", "https://broker.example.com")
    propose_action(
        conn, "routine", "trading_work",
        json.dumps([{"name": "journal_folder"}, {"name": "broker_site"}]), "reason",
    )
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    executed_name = confirm_pending_action(conn, confirmed=True)

    assert executed_name == "trading_work"
    assert opened == [
        ("folder", "E:/journal"),
        ("url", "https://broker.example.com"),
    ]
    assert get_action(conn, "trading_work") is not None


def test_execute_action_raises_action_execution_error_for_invalid_stored_kind(conn):
    from brain.automation import ActionExecutionError

    upsert_action(conn, "weird", "bogus_kind", "some-value")

    with pytest.raises(ActionExecutionError):
        execute_action(conn, "weird")


def test_execute_action_raises_action_execution_error_for_non_list_routine_json(conn):
    from brain.automation import ActionExecutionError

    upsert_action(conn, "broken_routine", "routine", "5")

    with pytest.raises(ActionExecutionError):
        execute_action(conn, "broken_routine")


def test_open_rejects_non_http_url_values():
    with pytest.raises(ValueError):
        automation._open("url", "C:/some/local/script.bat")


def test_open_raises_when_webbrowser_open_reports_failure(monkeypatch):
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda value: False)

    with pytest.raises(OSError):
        automation._open("url", "https://dead.example.com")


def test_run_one_off_action_opens_immediately(monkeypatch):
    from brain.automation import run_one_off_action

    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    run_one_off_action("url", "https://example.com")

    assert opened == [("url", "https://example.com")]


def test_run_one_off_action_propagates_open_failures():
    from brain.automation import run_one_off_action

    with pytest.raises(ValueError):
        run_one_off_action("url", "not-a-url")


def test_parse_routine_steps_accepts_minimal_step_objects():
    from brain.automation import _parse_routine_steps

    steps = _parse_routine_steps(json.dumps([{"name": "journal_folder"}]))

    assert steps == [
        {
            "name": "journal_folder",
            "new_window": False,
            "monitor": None,
            "maximized": False,
            "size": None,
            "position": None,
        }
    ]


def test_parse_routine_steps_accepts_new_window_and_monitor_overrides():
    from brain.automation import _parse_routine_steps

    steps = _parse_routine_steps(
        json.dumps([{"name": "tradingview_site", "new_window": True, "monitor": 1}])
    )

    assert steps == [
        {
            "name": "tradingview_site",
            "new_window": True,
            "monitor": 1,
            "maximized": False,
            "size": None,
            "position": None,
        }
    ]


def test_parse_routine_steps_rejects_a_step_with_no_name():
    from brain.automation import ActionExecutionError, _parse_routine_steps

    with pytest.raises(ActionExecutionError):
        _parse_routine_steps(json.dumps([{"new_window": True}]))


def test_parse_routine_steps_accepts_the_legacy_flat_string_format():
    # Routines saved by the earlier plain-string format (before this
    # feature) must keep working without a migration step — confirmed
    # against a real saved routine in the live database.
    from brain.automation import _parse_routine_steps

    steps = _parse_routine_steps(json.dumps(["journal_folder", "broker_site"]))

    assert steps == [
        {
            "name": "journal_folder",
            "new_window": False,
            "monitor": None,
            "maximized": False,
            "size": None,
            "position": None,
        },
        {
            "name": "broker_site",
            "new_window": False,
            "monitor": None,
            "maximized": False,
            "size": None,
            "position": None,
        },
    ]


def test_parse_routine_steps_rejects_a_list_item_that_is_neither_string_nor_object():
    from brain.automation import ActionExecutionError, _parse_routine_steps

    with pytest.raises(ActionExecutionError):
        _parse_routine_steps(json.dumps([42]))


def test_execute_action_routine_step_with_monitor_calls_open_placed(conn, monkeypatch):
    import brain.automation as automation

    upsert_action(conn, "tradingview_site", "url", "https://tradingview.com")
    upsert_action(
        conn, "trading_work", "routine",
        json.dumps([{"name": "tradingview_site", "new_window": True, "monitor": 1}]),
    )
    placed_calls = []
    monkeypatch.setattr(
        automation.window_manager,
        "open_placed",
        lambda kind, value, new_window, monitor, **kwargs: placed_calls.append(
            (kind, value, new_window, monitor)
        ),
    )
    opened_calls = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened_calls.append((kind, value)))

    summary = execute_action(conn, "trading_work")

    assert placed_calls == [("url", "https://tradingview.com", True, 1)]
    assert opened_calls == []
    assert "tradingview_site" in summary


def test_execute_action_routine_step_without_placement_calls_plain_open(conn, monkeypatch):
    import brain.automation as automation

    upsert_action(conn, "youtube_music", "url", "https://music.youtube.com")
    upsert_action(
        conn, "trading_work", "routine",
        json.dumps([{"name": "youtube_music"}]),
    )
    placed_calls = []
    monkeypatch.setattr(
        automation.window_manager,
        "open_placed",
        lambda kind, value, new_window, monitor, **kwargs: placed_calls.append(
            (kind, value, new_window, monitor)
        ),
    )
    opened_calls = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened_calls.append((kind, value)))

    execute_action(conn, "trading_work")

    assert opened_calls == [("url", "https://music.youtube.com")]
    assert placed_calls == []


def test_execute_action_routine_with_two_placed_steps_calls_open_placed_for_each_in_order(
    conn, monkeypatch
):
    import brain.automation as automation

    upsert_action(conn, "tradingview_site", "url", "https://tradingview.com")
    upsert_action(conn, "trading_journal", "url", "https://journal.example.com")
    upsert_action(
        conn, "trading_work", "routine",
        json.dumps([
            {"name": "tradingview_site", "new_window": True, "monitor": 1},
            {"name": "trading_journal", "new_window": True, "monitor": 2},
        ]),
    )
    placed_calls = []
    monkeypatch.setattr(
        automation.window_manager,
        "open_placed",
        lambda kind, value, new_window, monitor, **kwargs: placed_calls.append(
            (kind, value, new_window, monitor)
        ),
    )

    summary = execute_action(conn, "trading_work")

    # Each step must be placed with its own distinct args, in order — a
    # step must never be placed using the previous step's monitor/value.
    assert placed_calls == [
        ("url", "https://tradingview.com", True, 1),
        ("url", "https://journal.example.com", True, 2),
    ]
    assert "tradingview_site" in summary
    assert "trading_journal" in summary


def test_parse_routine_steps_accepts_maximized_and_size():
    from brain.automation import _parse_routine_steps

    steps = _parse_routine_steps(
        json.dumps([{"name": "tradingview_site", "maximized": True, "size": [0.5, 1.0], "position": None}])
    )

    assert steps == [
        {
            "name": "tradingview_site",
            "new_window": False,
            "monitor": None,
            "maximized": True,
            "size": [0.5, 1.0],
            "position": None,
        }
    ]


def test_parse_routine_steps_defaults_maximized_and_size_when_omitted():
    from brain.automation import _parse_routine_steps

    steps = _parse_routine_steps(json.dumps([{"name": "journal_folder"}]))

    assert steps == [
        {
            "name": "journal_folder",
            "new_window": False,
            "monitor": None,
            "maximized": False,
            "size": None,
            "position": None,
        }
    ]


def test_parse_routine_steps_rejects_non_boolean_maximized():
    from brain.automation import ActionExecutionError, _parse_routine_steps

    with pytest.raises(ActionExecutionError):
        _parse_routine_steps(json.dumps([{"name": "journal_folder", "maximized": "yes"}]))


def test_parse_routine_steps_rejects_malformed_size():
    from brain.automation import ActionExecutionError, _parse_routine_steps

    with pytest.raises(ActionExecutionError):
        _parse_routine_steps(json.dumps([{"name": "journal_folder", "size": [1], "position": None}]))


def test_execute_action_routine_step_with_maximized_calls_open_placed(conn, monkeypatch):
    import brain.automation as automation

    upsert_action(conn, "tradingview_site", "url", "https://tradingview.com")
    upsert_action(
        conn, "trading_work", "routine",
        json.dumps([{"name": "tradingview_site", "maximized": True}]),
    )
    calls = []
    monkeypatch.setattr(
        automation.window_manager,
        "open_placed",
        lambda kind, value, new_window, monitor, **kwargs: calls.append(
            (kind, value, new_window, monitor, kwargs)
        ),
    )

    execute_action(conn, "trading_work")

    assert calls == [
        ("url", "https://tradingview.com", False, None, {"maximized": True, "size": None, "position": None})
    ]


def test_execute_action_routine_step_with_size_calls_open_placed(conn, monkeypatch):
    import brain.automation as automation

    upsert_action(conn, "tradingview_site", "url", "https://tradingview.com")
    upsert_action(
        conn, "trading_work", "routine",
        json.dumps([{"name": "tradingview_site", "monitor": 1, "size": [0.5, 0.5], "position": None}]),
    )
    calls = []
    monkeypatch.setattr(
        automation.window_manager,
        "open_placed",
        lambda kind, value, new_window, monitor, **kwargs: calls.append(
            (kind, value, new_window, monitor, kwargs)
        ),
    )

    execute_action(conn, "trading_work")

    assert calls == [
        ("url", "https://tradingview.com", False, 1, {"maximized": False, "size": [0.5, 0.5], "position": None})
    ]


def test_run_ad_hoc_placement_executes_immediately_without_saving(conn, monkeypatch):
    from brain.automation import run_ad_hoc_placement

    upsert_action(conn, "tradingview", "url", "https://tradingview.com")
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    steps_json = json.dumps([{"name": "tradingview", "monitor": 1, "size": [0.5, 0.5], "position": None}])
    placed_calls = []
    monkeypatch.setattr(
        automation.window_manager,
        "open_placed",
        lambda kind, value, new_window, monitor, **kwargs: placed_calls.append(
            (kind, value, new_window, monitor, kwargs)
        ),
    )

    summary = run_ad_hoc_placement(conn, steps_json)

    assert placed_calls == [
        ("url", "https://tradingview.com", False, 1, {"maximized": False, "size": [0.5, 0.5], "position": None})
    ]
    assert opened == []
    assert "tradingview" in summary
    # Nothing new should be persisted — this is a one-off, not a saved routine.
    assert get_action(conn, "(ad hoc)") is None


def test_run_ad_hoc_placement_executes_a_step_with_no_placement_via_plain_open(conn, monkeypatch):
    from brain.automation import run_ad_hoc_placement

    upsert_action(conn, "tradingview", "url", "https://tradingview.com")
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    steps_json = json.dumps([{"name": "tradingview"}])

    run_ad_hoc_placement(conn, steps_json)

    assert opened == [("url", "https://tradingview.com")]


def test_run_ad_hoc_placement_raises_action_not_found_for_unknown_step(conn):
    from brain.automation import ActionNotFoundError, run_ad_hoc_placement

    steps_json = json.dumps([{"name": "ghost_action"}])

    with pytest.raises(ActionNotFoundError) as exc_info:
        run_ad_hoc_placement(conn, steps_json)

    assert "ghost_action" in str(exc_info.value)


def test_run_ad_hoc_placement_raises_action_not_found_when_any_step_is_unknown(conn, monkeypatch):
    from brain.automation import ActionNotFoundError, run_ad_hoc_placement

    upsert_action(conn, "tradingview", "url", "https://tradingview.com")
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    steps_json = json.dumps([{"name": "tradingview"}, {"name": "ghost_action"}])

    with pytest.raises(ActionNotFoundError):
        run_ad_hoc_placement(conn, steps_json)

    # Nothing should have been opened — validated before executing anything.
    assert opened == []


def test_run_ad_hoc_placement_raises_action_execution_error_for_malformed_steps(conn):
    from brain.automation import ActionExecutionError, run_ad_hoc_placement

    with pytest.raises(ActionExecutionError):
        run_ad_hoc_placement(conn, "not valid json")


def test_open_raises_oserror_for_app_kind_when_automation_not_supported(monkeypatch):
    monkeypatch.setattr(automation, "AUTOMATION_SUPPORTED", False)

    with pytest.raises(OSError, match="not available on this platform"):
        automation._open("app", "C:/apps/code.exe")


def test_open_raises_oserror_for_folder_kind_when_automation_not_supported(monkeypatch):
    monkeypatch.setattr(automation, "AUTOMATION_SUPPORTED", False)

    with pytest.raises(OSError, match="not available on this platform"):
        automation._open("folder", "E:/journal")


def test_open_still_handles_url_kind_when_automation_not_supported(monkeypatch):
    monkeypatch.setattr(automation, "AUTOMATION_SUPPORTED", False)
    opened = []
    monkeypatch.setattr(automation.webbrowser, "open", lambda url: opened.append(url) or True)

    automation._open("url", "https://example.com")

    assert opened == ["https://example.com"]


def test_confirm_pending_action_saves_without_executing_when_automation_not_supported(
    conn, monkeypatch
):
    from brain.automation import (
        ActionSavedButNotExecutedError,
        confirm_pending_action,
        get_action,
        propose_action,
    )
    from brain.memory import get_pending_proposal

    monkeypatch.setattr(automation, "AUTOMATION_SUPPORTED", False)
    propose_action(conn, "folder", "journal_folder", "E:/journal", "reason")

    with pytest.raises(ActionSavedButNotExecutedError) as exc_info:
        confirm_pending_action(conn, confirmed=True)

    assert exc_info.value.name == "journal_folder"
    assert get_action(conn, "journal_folder") == {
        "name": "journal_folder",
        "kind": "folder",
        "value": "E:/journal",
    }
    assert get_pending_proposal(conn) is None


def test_confirm_pending_action_still_attempts_url_kind_when_automation_not_supported(
    conn, monkeypatch
):
    from brain.automation import ActionExecutionError, confirm_pending_action, propose_action

    monkeypatch.setattr(automation, "AUTOMATION_SUPPORTED", False)
    monkeypatch.setattr(automation.webbrowser, "open", lambda url: False)
    propose_action(conn, "url", "quickbrew_site", "https://quickbrew.com", "reason")

    with pytest.raises(ActionExecutionError, match="failed to open url"):
        confirm_pending_action(conn, confirmed=True)
