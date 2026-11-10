import pytest
import requests


def test_ends_with_test_suffix_accepts_a_test_database_name():
    from tests.conftest import _ends_with_test_suffix

    assert _ends_with_test_suffix("postgresql://postgres:pw@host:5432/jarvis_test") is True


def test_ends_with_test_suffix_rejects_a_production_looking_database_name():
    from tests.conftest import _ends_with_test_suffix

    assert _ends_with_test_suffix("postgresql://postgres:pw@host:5432/jarvis") is False


def test_a_real_unmocked_http_request_fails_loudly_instead_of_hitting_the_network():
    # Guards against a repeat of the bug where a weather fetch that should
    # have been mocked slipped through and hit the real Open-Meteo API from
    # a test. Any code path that reaches requests' real Session.request
    # without the test having mocked requests.get/post at the module level
    # must blow up immediately and obviously, not silently make a network
    # call.
    with pytest.raises(AssertionError, match="real HTTP request"):
        requests.get("https://example.com", timeout=1)
