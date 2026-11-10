from datetime import date, datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
import requests


def test_truncate_returns_text_unchanged_when_under_the_limit():
    from brain.web_tools import _truncate

    assert _truncate("short text") == "short text"


def test_truncate_cuts_long_text_and_marks_it():
    from brain.web_tools import _truncate

    result = _truncate("a" * 5000, max_chars=100)

    assert len(result) < 5000
    assert result.startswith("a" * 100)
    assert "truncated" in result


def test_looks_like_url_accepts_http_and_https():
    from brain.web_tools import _looks_like_url

    assert _looks_like_url("https://example.com/page") is True
    assert _looks_like_url("http://example.com") is True


def test_looks_like_url_rejects_non_urls():
    from brain.web_tools import _looks_like_url

    assert _looks_like_url("not a url") is False
    assert _looks_like_url("example.com") is False
    assert _looks_like_url("") is False


def test_read_webpage_returns_page_text_on_success():
    from brain.web_tools import read_webpage

    fake_response = Mock(text="This is the page content.")
    fake_response.raise_for_status = Mock()
    with patch("brain.web_tools.requests.get", return_value=fake_response) as mock_get:
        result = read_webpage("https://example.com/article")

    mock_get.assert_called_once_with(
        "https://r.jina.ai/https://example.com/article", timeout=15
    )
    assert result == "This is the page content."


def test_read_webpage_truncates_a_very_long_page():
    from brain.web_tools import read_webpage

    fake_response = Mock(text="x" * 10000)
    fake_response.raise_for_status = Mock()
    with patch("brain.web_tools.requests.get", return_value=fake_response):
        result = read_webpage("https://example.com/long-article")

    assert len(result) < 10000
    assert "truncated" in result


def test_read_webpage_returns_friendly_message_on_network_failure():
    from brain.web_tools import read_webpage

    with patch(
        "brain.web_tools.requests.get",
        side_effect=requests.exceptions.ConnectionError("boom"),
    ):
        result = read_webpage("https://example.com")

    assert "couldn't reach" in result.lower()


def test_read_webpage_returns_friendly_message_for_invalid_url():
    from brain.web_tools import read_webpage

    result = read_webpage("not a url")

    assert "valid url" in result.lower()


def test_read_webpage_tool_has_the_expected_name():
    from brain.web_tools import READ_WEBPAGE_TOOL

    assert READ_WEBPAGE_TOOL["name"] == "read_webpage"


def test_get_github_repo_returns_summary_on_success():
    from brain.web_tools import get_github_repo

    fake_response = Mock(status_code=200)
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {
        "full_name": "torvalds/linux",
        "description": "Linux kernel source tree",
        "stargazers_count": 190000,
        "language": "C",
        "pushed_at": "2026-09-20T00:00:00Z",
    }
    with patch("brain.web_tools.requests.get", return_value=fake_response) as mock_get:
        result = get_github_repo("torvalds/linux")

    mock_get.assert_called_once_with(
        "https://api.github.com/repos/torvalds/linux",
        headers={"Accept": "application/vnd.github+json"},
        timeout=15,
    )
    assert "torvalds/linux" in result
    assert "190000" in result
    assert "C" in result


def test_get_github_repo_returns_friendly_message_on_404():
    from brain.web_tools import get_github_repo

    fake_response = Mock(status_code=404)
    with patch("brain.web_tools.requests.get", return_value=fake_response):
        result = get_github_repo("nobody/nothing")

    assert "couldn't find" in result.lower()


def test_get_github_repo_strips_a_full_github_url():
    from brain.web_tools import get_github_repo

    fake_response = Mock(status_code=200)
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {
        "full_name": "octocat/Hello-World",
        "description": None,
        "stargazers_count": 1,
        "language": None,
        "pushed_at": "2026-01-01T00:00:00Z",
    }
    with patch("brain.web_tools.requests.get", return_value=fake_response) as mock_get:
        result = get_github_repo("https://github.com/octocat/Hello-World")

    mock_get.assert_called_once_with(
        "https://api.github.com/repos/octocat/Hello-World",
        headers={"Accept": "application/vnd.github+json"},
        timeout=15,
    )
    assert "octocat/Hello-World" in result


def test_get_github_repo_returns_friendly_message_for_malformed_input():
    from brain.web_tools import get_github_repo

    result = get_github_repo("just-a-name")

    assert "owner/repo" in result.lower()


def test_get_github_repo_returns_friendly_message_on_network_failure():
    from brain.web_tools import get_github_repo

    with patch(
        "brain.web_tools.requests.get",
        side_effect=requests.exceptions.ConnectionError("boom"),
    ):
        result = get_github_repo("torvalds/linux")

    assert "couldn't reach" in result.lower()


def test_get_github_repo_tool_has_the_expected_name():
    from brain.web_tools import GET_GITHUB_REPO_TOOL

    assert GET_GITHUB_REPO_TOOL["name"] == "get_github_repo"


def _fake_feed(entries, bozo=False):
    feed = Mock()
    feed.bozo = bozo
    feed.entries = entries
    feed.get = lambda key, default=None: default
    return feed


def _fake_feed_fetch_response():
    response = Mock(status_code=200, content=b"<rss></rss>")
    response.raise_for_status = Mock()
    return response


def test_read_rss_feed_returns_recent_entries_on_success():
    from brain.web_tools import read_rss_feed

    entries = [
        {"title": "First post", "summary": "About the first thing."},
        {"title": "Second post", "summary": "About the second thing."},
    ]
    with (
        patch("brain.web_tools.requests.get", return_value=_fake_feed_fetch_response()),
        patch("brain.web_tools.feedparser.parse", return_value=_fake_feed(entries)),
    ):
        result = read_rss_feed("https://example.com/feed.xml")

    assert "First post" in result
    assert "Second post" in result


def test_read_rss_feed_returns_friendly_message_when_feed_is_empty():
    from brain.web_tools import read_rss_feed

    with (
        patch("brain.web_tools.requests.get", return_value=_fake_feed_fetch_response()),
        patch("brain.web_tools.feedparser.parse", return_value=_fake_feed([])),
    ):
        result = read_rss_feed("https://example.com/empty.xml")

    assert "no" in result.lower() or "entries" in result.lower()


def test_read_rss_feed_returns_friendly_message_when_parsing_fails():
    from brain.web_tools import read_rss_feed

    with (
        patch("brain.web_tools.requests.get", return_value=_fake_feed_fetch_response()),
        patch("brain.web_tools.feedparser.parse", return_value=_fake_feed([], bozo=True)),
    ):
        result = read_rss_feed("https://example.com/broken.xml")

    assert "couldn't read" in result.lower()


def test_read_rss_feed_returns_friendly_message_for_invalid_url():
    from brain.web_tools import read_rss_feed

    result = read_rss_feed("not a url")

    assert "valid url" in result.lower()


def test_read_rss_feed_tool_has_the_expected_name():
    from brain.web_tools import READ_RSS_FEED_TOOL

    assert READ_RSS_FEED_TOOL["name"] == "read_rss_feed"


def _fake_geo_response(results):
    response = Mock(status_code=200)
    response.raise_for_status = Mock()
    response.json.return_value = {"results": results}
    return response


def _fake_forecast_response(temperature, code):
    response = Mock(status_code=200)
    response.raise_for_status = Mock()
    response.json.return_value = {
        "current": {"temperature_2m": temperature, "weather_code": code}
    }
    return response


def test_get_weather_returns_forecast_for_explicit_location(conn):
    from brain.web_tools import get_weather

    geo = _fake_geo_response([{"latitude": 35.68, "longitude": 139.69, "name": "Tokyo"}])
    forecast = _fake_forecast_response(22.5, 1)
    with patch("brain.web_tools.requests.get", side_effect=[geo, forecast]):
        result = get_weather(conn, "Tokyo")

    assert "Tokyo" in result
    assert "22.5" in result


def test_get_weather_falls_back_to_remembered_location_fact(conn):
    from brain.memory import upsert_fact
    from brain.web_tools import get_weather

    upsert_fact(conn, "location", "Berlin")
    geo = _fake_geo_response([{"latitude": 52.52, "longitude": 13.40, "name": "Berlin"}])
    forecast = _fake_forecast_response(15.0, 3)
    with patch("brain.web_tools.requests.get", side_effect=[geo, forecast]):
        result = get_weather(conn, None)

    assert "Berlin" in result


def test_get_weather_asks_for_a_city_when_none_given_and_none_remembered(conn):
    from brain.web_tools import get_weather

    result = get_weather(conn, None)

    assert "which city" in result.lower()


def test_get_weather_does_not_overwrite_the_remembered_location_fact(conn):
    from brain.memory import get_all_facts, upsert_fact
    from brain.web_tools import get_weather

    upsert_fact(conn, "location", "Berlin")
    geo = _fake_geo_response([{"latitude": 35.68, "longitude": 139.69, "name": "Tokyo"}])
    forecast = _fake_forecast_response(22.5, 1)
    with patch("brain.web_tools.requests.get", side_effect=[geo, forecast]):
        get_weather(conn, "Tokyo")

    assert get_all_facts(conn)["location"] == "Berlin"


def test_get_weather_returns_friendly_message_when_city_not_found(conn):
    from brain.web_tools import get_weather

    with patch("brain.web_tools.requests.get", return_value=_fake_geo_response([])):
        result = get_weather(conn, "Nowhereville")

    assert "couldn't find" in result.lower()


def test_get_weather_returns_friendly_message_on_network_failure(conn):
    from brain.web_tools import get_weather

    with patch(
        "brain.web_tools.requests.get",
        side_effect=requests.exceptions.ConnectionError("boom"),
    ):
        result = get_weather(conn, "Tokyo")

    assert "couldn't reach" in result.lower()


def test_get_weather_tool_has_the_expected_name():
    from brain.web_tools import GET_WEATHER_TOOL

    assert GET_WEATHER_TOOL["name"] == "get_weather"


def test_select_caption_url_prefers_english_json3_track():
    from brain.web_tools import _select_caption_url

    info = {
        "automatic_captions": {
            "en": [
                {"ext": "vtt", "url": "https://example.com/en.vtt"},
                {"ext": "json3", "url": "https://example.com/en.json3"},
            ]
        }
    }

    assert _select_caption_url(info) == "https://example.com/en.json3"


def test_select_caption_url_returns_none_when_no_captions_available():
    from brain.web_tools import _select_caption_url

    assert _select_caption_url({"automatic_captions": {}}) is None
    assert _select_caption_url({}) is None


def test_fetch_transcript_text_joins_json3_segments():
    from brain.web_tools import _fetch_transcript_text

    fake_response = Mock()
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {
        "events": [
            {"segs": [{"utf8": "Hello "}, {"utf8": "world."}]},
            {"segs": [{"utf8": " More text."}]},
        ]
    }
    with patch("brain.web_tools.requests.get", return_value=fake_response):
        result = _fetch_transcript_text("https://example.com/captions.json3")

    assert result == "Hello world. More text."


def test_get_youtube_info_returns_metadata_and_transcript_on_success():
    from brain.web_tools import get_youtube_info

    fake_info = {
        "title": "A Great Video",
        "uploader": "Some Channel",
        "duration": 125,
        "automatic_captions": {"en": [{"ext": "json3", "url": "https://example.com/c.json3"}]},
    }
    with (
        patch("brain.web_tools.yt_dlp.YoutubeDL") as mock_ydl_cls,
        patch("brain.web_tools._fetch_transcript_text", return_value="This video is about testing."),
    ):
        mock_ydl_cls.return_value.__enter__.return_value.extract_info.return_value = fake_info
        result = get_youtube_info("https://youtube.com/watch?v=abc123")

    assert "A Great Video" in result
    assert "Some Channel" in result
    assert "2m 5s" in result
    assert "This video is about testing." in result


def test_get_youtube_info_degrades_gracefully_when_no_captions_available():
    from brain.web_tools import get_youtube_info

    fake_info = {
        "title": "A Silent Video",
        "uploader": "Some Channel",
        "duration": 60,
        "automatic_captions": {},
    }
    with patch("brain.web_tools.yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl_cls.return_value.__enter__.return_value.extract_info.return_value = fake_info
        result = get_youtube_info("https://youtube.com/watch?v=noCaptions")

    assert "A Silent Video" in result
    assert "no transcript" in result.lower() or "no captions" in result.lower()


def test_get_youtube_info_returns_friendly_message_on_extraction_failure():
    from brain.web_tools import get_youtube_info
    import yt_dlp

    with patch("brain.web_tools.yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl_cls.return_value.__enter__.return_value.extract_info.side_effect = (
            yt_dlp.utils.DownloadError("video unavailable")
        )
        result = get_youtube_info("https://youtube.com/watch?v=gone")

    assert "couldn't" in result.lower()


def test_get_youtube_info_returns_friendly_message_for_invalid_url():
    from brain.web_tools import get_youtube_info

    result = get_youtube_info("not a url")

    assert "valid url" in result.lower()


def test_get_youtube_info_tool_has_the_expected_name():
    from brain.web_tools import GET_YOUTUBE_INFO_TOOL

    assert GET_YOUTUBE_INFO_TOOL["name"] == "get_youtube_info"


def test_web_search_returns_results_on_success(monkeypatch):
    from brain.web_tools import web_search

    monkeypatch.setattr("brain.web_tools.resolve_exa_api_key", lambda: "exa-key")
    fake_response = Mock()
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {
        "results": [
            {"title": "Result One", "url": "https://example.com/one"},
            {"title": "Result Two", "url": "https://example.com/two"},
        ]
    }
    with patch("brain.web_tools.requests.post", return_value=fake_response) as mock_post:
        result = web_search("best pizza in town")

    assert "Result One" in result
    assert "Result Two" in result
    called_headers = mock_post.call_args.kwargs["headers"]
    assert called_headers["x-api-key"] == "exa-key"


def test_web_search_returns_friendly_message_when_no_api_key_configured(monkeypatch):
    from brain.web_tools import web_search

    monkeypatch.setattr("brain.web_tools.resolve_exa_api_key", lambda: None)

    result = web_search("anything")

    assert "not configured" in result.lower() or "isn't configured" in result.lower()


def test_web_search_returns_friendly_message_when_no_results(monkeypatch):
    from brain.web_tools import web_search

    monkeypatch.setattr("brain.web_tools.resolve_exa_api_key", lambda: "exa-key")
    fake_response = Mock()
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {"results": []}
    with patch("brain.web_tools.requests.post", return_value=fake_response):
        result = web_search("something obscure")

    assert "didn't find" in result.lower()


def test_web_search_returns_friendly_message_on_network_failure(monkeypatch):
    from brain.web_tools import web_search

    monkeypatch.setattr("brain.web_tools.resolve_exa_api_key", lambda: "exa-key")
    with patch(
        "brain.web_tools.requests.post",
        side_effect=requests.exceptions.ConnectionError("boom"),
    ):
        result = web_search("anything")

    assert "couldn't search" in result.lower()


def test_web_search_tool_has_the_expected_name():
    from brain.web_tools import WEB_SEARCH_TOOL

    assert WEB_SEARCH_TOOL["name"] == "web_search"


# --- Final-review fixes ---


def test_read_rss_feed_fetches_with_a_timeout_before_parsing():
    from brain.web_tools import read_rss_feed

    fake_response = Mock(status_code=200, content=b"<rss></rss>")
    fake_response.raise_for_status = Mock()
    with (
        patch("brain.web_tools.requests.get", return_value=fake_response) as mock_get,
        patch(
            "brain.web_tools.feedparser.parse",
            return_value=_fake_feed([{"title": "A", "summary": "B"}]),
        ),
    ):
        read_rss_feed("https://example.com/feed.xml")

    mock_get.assert_called_once_with("https://example.com/feed.xml", timeout=15)


def test_read_rss_feed_returns_friendly_message_on_fetch_failure_without_calling_feedparser():
    from brain.web_tools import read_rss_feed

    with (
        patch(
            "brain.web_tools.requests.get",
            side_effect=requests.exceptions.ConnectionError("boom"),
        ),
        patch("brain.web_tools.feedparser.parse") as mock_parse,
    ):
        result = read_rss_feed("https://example.com/feed.xml")

    assert "couldn't read" in result.lower()
    mock_parse.assert_not_called()


def test_get_github_repo_returns_friendly_message_when_response_is_not_json():
    from brain.web_tools import get_github_repo

    fake_response = Mock(status_code=200)
    fake_response.raise_for_status = Mock()
    fake_response.json.side_effect = ValueError("not json")
    with patch("brain.web_tools.requests.get", return_value=fake_response):
        result = get_github_repo("torvalds/linux")

    assert "couldn't reach" in result.lower()


def test_web_search_returns_friendly_message_when_response_is_not_json(monkeypatch):
    from brain.web_tools import web_search

    monkeypatch.setattr("brain.web_tools.resolve_exa_api_key", lambda: "exa-key")
    fake_response = Mock()
    fake_response.raise_for_status = Mock()
    fake_response.json.side_effect = ValueError("not json")
    with patch("brain.web_tools.requests.post", return_value=fake_response):
        result = web_search("anything")

    assert "couldn't search" in result.lower()


def test_web_search_includes_a_content_snippet_when_available(monkeypatch):
    from brain.web_tools import web_search

    monkeypatch.setattr("brain.web_tools.resolve_exa_api_key", lambda: "exa-key")
    fake_response = Mock()
    fake_response.raise_for_status = Mock()
    fake_response.json.return_value = {
        "results": [
            {
                "title": "Result One",
                "url": "https://example.com/one",
                "text": "A short snippet about pizza.",
            },
        ]
    }
    with patch("brain.web_tools.requests.post", return_value=fake_response) as mock_post:
        result = web_search("best pizza")

    assert "A short snippet about pizza." in result
    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["contents"] == {"text": {"maxCharacters": 500}}


def test_get_weather_returns_friendly_message_when_forecast_response_is_not_json(conn):
    from brain.web_tools import get_weather

    geo = _fake_geo_response([{"latitude": 35.68, "longitude": 139.69, "name": "Tokyo"}])
    forecast = Mock(status_code=200)
    forecast.raise_for_status = Mock()
    forecast.json.side_effect = ValueError("not json")
    with patch("brain.web_tools.requests.get", side_effect=[geo, forecast]):
        result = get_weather(conn, "Tokyo")

    assert "couldn't reach" in result.lower()


def test_get_weather_returns_friendly_message_when_geocoding_result_missing_fields(conn):
    from brain.web_tools import get_weather

    geo = _fake_geo_response([{"name": "Tokyo"}])
    with patch("brain.web_tools.requests.get", return_value=geo):
        result = get_weather(conn, "Tokyo")

    assert "couldn't reach" in result.lower()


def test_get_youtube_info_returns_friendly_message_when_extraction_returns_none():
    from brain.web_tools import get_youtube_info

    with patch("brain.web_tools.yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl_cls.return_value.__enter__.return_value.extract_info.return_value = None
        result = get_youtube_info("https://youtube.com/watch?v=deleted")

    assert "couldn't" in result.lower()


# --- geocode cache -------------------------------------------------------


def test_geocode_caches_a_successful_lookup_across_calls():
    from brain.web_tools import _geocode

    geo = _fake_geo_response(
        [{"latitude": 41.6, "longitude": 41.6, "name": "Lisbon", "timezone": "Asia/Tbilisi"}]
    )
    with patch("brain.web_tools.requests.get", return_value=geo) as mock_get:
        first = _geocode("Lisbon, Portugal")
        second = _geocode("Lisbon, Portugal")

    assert first == second == ("Lisbon", 41.6, 41.6, "Asia/Tbilisi")
    mock_get.assert_called_once()


def test_geocode_does_not_cache_a_failed_lookup():
    from brain.web_tools import _GeocodeNotFound, _geocode

    with patch("brain.web_tools.requests.get", return_value=_fake_geo_response([])) as mock_get:
        with pytest.raises(_GeocodeNotFound):
            _geocode("Nowhereville")
        with pytest.raises(_GeocodeNotFound):
            _geocode("Nowhereville")

    assert mock_get.call_count == 2


def test_geocode_does_not_cache_a_result_without_a_timezone():
    from brain.web_tools import _geocode

    # No "timezone" field — a degraded geocoding result. Caching it would
    # permanently deny forecast_hour_line the chance to get a proper
    # timezone on a later, better-answered lookup.
    geo = _fake_geo_response([{"latitude": 1.0, "longitude": 2.0, "name": "Place"}])
    with patch("brain.web_tools.requests.get", return_value=geo) as mock_get:
        _geocode("Place")
        _geocode("Place")

    assert mock_get.call_count == 2


# --- forecast_day_line ----------------------------------------------------


def _hourly_times(day="2026-09-27"):
    return [f"{day}T{h:02d}:00" for h in range(24)]


def _fake_day_forecast_response(temperature, precipitation_probability, weather_code, times=None):
    response = Mock(status_code=200)
    response.raise_for_status = Mock()
    response.json.return_value = {
        "hourly": {
            "time": times or _hourly_times(),
            "temperature_2m": temperature,
            "precipitation_probability": precipitation_probability,
            "weather_code": weather_code,
        }
    }
    return response


def _city_geo():
    return _fake_geo_response([{"latitude": 41.65, "longitude": 41.64, "name": "Lisbon"}])


def test_forecast_day_line_rounds_min_and_max_temperature_and_reports_no_rain():
    from brain.web_tools import forecast_day_line

    temps = [18.0] * 24
    temps[3] = 15.6  # rounds to 16 (min)
    temps[15] = 22.3  # rounds to 22 (max)
    precip = [10] * 24
    codes = [0] * 24
    forecast = _fake_day_forecast_response(temps, precip, codes)
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert result == "☀️ Lisbon today: 16–22°C · no rain expected"


def test_forecast_day_line_reports_a_single_rain_range():
    from brain.web_tools import forecast_day_line

    temps = [18.0] * 24
    precip = [10] * 24
    for h in (14, 15, 16):
        precip[h] = 60
    codes = [61] * 24
    forecast = _fake_day_forecast_response(temps, precip, codes)
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert "rain likely 14:00–17:00" in result


def test_forecast_day_line_reports_two_rain_ranges():
    from brain.web_tools import forecast_day_line

    temps = [18.0] * 24
    precip = [10] * 24
    for h in (8, 9):
        precip[h] = 60
    for h in (14, 15, 16):
        precip[h] = 60
    codes = [61] * 24
    forecast = _fake_day_forecast_response(temps, precip, codes)
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert "rain likely 08:00–10:00, 14:00–17:00" in result
    assert "…" not in result


def test_forecast_day_line_truncates_a_third_rain_range_with_an_ellipsis():
    from brain.web_tools import forecast_day_line

    temps = [18.0] * 24
    precip = [10] * 24
    for h in (6, 7):
        precip[h] = 60
    for h in (12, 13):
        precip[h] = 60
    for h in (18, 19):
        precip[h] = 60
    codes = [61] * 24
    forecast = _fake_day_forecast_response(temps, precip, codes)
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert "rain likely 06:00–08:00, 12:00–14:00…" in result
    assert "18:00" not in result


@pytest.mark.parametrize(
    "weather_code, expected_emoji",
    [
        (3, "☁️"),
        (45, "🌫️"),
        (71, "🌨️"),
        (95, "⛈️"),
    ],
)
def test_forecast_day_line_picks_emoji_for_a_uniform_condition(weather_code, expected_emoji):
    from brain.web_tools import forecast_day_line

    temps = [18.0] * 24
    precip = [10] * 24
    codes = [weather_code] * 24
    forecast = _fake_day_forecast_response(temps, precip, codes)
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert result.startswith(expected_emoji)


def test_forecast_day_line_uses_mixed_emoji_when_sun_and_rain_both_occur():
    from brain.web_tools import forecast_day_line

    temps = [18.0] * 24
    precip = [10] * 12 + [60] * 12
    codes = [0] * 12 + [61] * 12
    forecast = _fake_day_forecast_response(temps, precip, codes)
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert result.startswith("🌦️")


def test_forecast_day_line_returns_none_on_timeout():
    from brain.web_tools import forecast_day_line

    with patch(
        "brain.web_tools.requests.get",
        side_effect=[_city_geo(), requests.exceptions.Timeout("slow")],
    ):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert result is None


def test_forecast_day_line_returns_none_on_http_error():
    from brain.web_tools import forecast_day_line

    broken = Mock(status_code=500)
    broken.raise_for_status = Mock(side_effect=requests.exceptions.HTTPError("boom"))
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), broken]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert result is None


def test_forecast_day_line_returns_none_when_geocode_finds_nothing():
    from brain.web_tools import forecast_day_line

    with patch("brain.web_tools.requests.get", return_value=_fake_geo_response([])):
        result = forecast_day_line("Nowhereville", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert result is None


def test_forecast_day_line_returns_none_when_hourly_keys_are_missing():
    from brain.web_tools import forecast_day_line

    broken = Mock(status_code=200)
    broken.raise_for_status = Mock()
    broken.json.return_value = {"hourly": {"temperature_2m": [18.0] * 24}}
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), broken]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert result is None


def test_forecast_day_line_returns_none_when_every_temperature_is_null():
    from brain.web_tools import forecast_day_line

    temps = [None] * 24
    precip = [10] * 24
    codes = [0] * 24
    forecast = _fake_day_forecast_response(temps, precip, codes)
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert result is None


def test_forecast_day_line_ignores_null_temperatures_when_computing_min_and_max():
    from brain.web_tools import forecast_day_line

    temps = [None] * 24
    temps[3] = 15.6  # rounds to 16 (min)
    temps[15] = 22.3  # rounds to 22 (max)
    precip = [10] * 24
    codes = [0] * 24
    forecast = _fake_day_forecast_response(temps, precip, codes)
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert result == "☀️ Lisbon today: 16–22°C · no rain expected"


def test_forecast_day_line_shows_a_single_temperature_when_min_equals_max():
    from brain.web_tools import forecast_day_line

    temps = [20.0] * 24
    precip = [10] * 24
    codes = [0] * 24
    forecast = _fake_day_forecast_response(temps, precip, codes)
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert "20°C" in result
    assert "20–20°C" not in result


def test_forecast_day_line_reports_rain_running_to_midnight_as_from_time():
    from brain.web_tools import forecast_day_line

    temps = [18.0] * 24
    precip = [10] * 24
    for h in range(17, 24):
        precip[h] = 60
    codes = [61] * 24
    forecast = _fake_day_forecast_response(temps, precip, codes)
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert "rain likely from 17:00" in result
    assert "24:00" not in result


def test_forecast_day_line_never_shows_a_rain_emoji_without_a_rain_hour():
    from brain.web_tools import forecast_day_line

    temps = [18.0] * 24
    precip = [10] * 24  # never crosses the 50% threshold
    codes = [61] * 24  # but weather_code claims rain all day
    forecast = _fake_day_forecast_response(temps, precip, codes)
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    assert "no rain expected" in result
    assert not result.startswith("🌦️")
    assert not result.startswith("🌧️")


def test_forecast_day_line_ignores_nighttime_hours_when_choosing_the_emoji():
    from brain.web_tools import forecast_day_line

    temps = [18.0] * 24
    precip = [10] * 24
    codes = [0] * 24  # clear overnight
    for h in range(7, 22):  # daytime: rain
        codes[h] = 61
        precip[h] = 60
    forecast = _fake_day_forecast_response(temps, precip, codes)
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]):
        result = forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    # Not the mixed 🌦️ — the only "clear" hours are all outside 07-21.
    assert result.startswith("🌧️")


def test_forecast_day_line_uses_a_shorter_request_timeout():
    from brain.web_tools import forecast_day_line

    forecast = _fake_day_forecast_response([18.0] * 24, [10] * 24, [0] * 24)
    with patch(
        "brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]
    ) as mock_get:
        forecast_day_line("Lisbon, Portugal", date(2026, 9, 27), ZoneInfo("Asia/Tbilisi"))

    for call in mock_get.call_args_list:
        assert call.kwargs["timeout"] == (3, 5)


@pytest.mark.parametrize(
    "weather_code, expected_emoji",
    [
        (56, "🌧️"), (57, "🌧️"), (66, "🌧️"), (67, "🌧️"),
        (77, "🌨️"), (85, "🌨️"), (86, "🌨️"),
        (96, "⛈️"), (99, "⛈️"),
    ],
)
def test_day_emoji_recognizes_additional_wmo_codes(weather_code, expected_emoji):
    from brain.web_tools import _day_emoji

    times = _hourly_times()
    codes = [weather_code] * 24

    assert _day_emoji(times, codes, has_rain=True) == expected_emoji


# --- forecast_hour_line ---------------------------------------------------


def _fake_hour_forecast_response(temperature, apparent, precipitation, wind, weather_code, times=None):
    response = Mock(status_code=200)
    response.raise_for_status = Mock()
    response.json.return_value = {
        "hourly": {
            "time": times or _hourly_times(),
            "temperature_2m": temperature,
            "apparent_temperature": apparent,
            "precipitation_probability": precipitation,
            "wind_speed_10m": wind,
            "weather_code": weather_code,
        }
    }
    return response


def _city_geo_with_tz():
    return _fake_geo_response(
        [{"latitude": 41.65, "longitude": 41.64, "name": "Lisbon", "timezone": "Asia/Tbilisi"}]
    )


def test_forecast_hour_line_selects_the_correct_local_hour():
    from brain.web_tools import forecast_hour_line

    temps = [10.0] * 24
    apparent = [10.0] * 24
    precip = [0] * 24
    wind = [5.0] * 24
    codes = [0] * 24
    temps[14], apparent[14], precip[14], wind[14], codes[14] = 14.0, 12.0, 40, 18.3, 61

    forecast = _fake_hour_forecast_response(temps, apparent, precip, wind, codes)
    # 10:00 UTC is 14:00 in Asia/Tbilisi (UTC+4, no DST).
    at = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("UTC"))
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo_with_tz(), forecast]):
        result = forecast_hour_line("Lisbon, Portugal", at)

    assert result == "🌧️ 14°C (feels 12°C) · rain 40% · wind 18 km/h"


def test_forecast_hour_line_omits_feels_like_when_equal_to_actual_temperature():
    from brain.web_tools import forecast_hour_line

    temps = [14.0] * 24
    apparent = [14.0] * 24
    precip = [40] * 24
    wind = [18.0] * 24
    codes = [61] * 24

    forecast = _fake_hour_forecast_response(temps, apparent, precip, wind, codes)
    at = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("UTC"))
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo_with_tz(), forecast]):
        result = forecast_hour_line("Lisbon, Portugal", at)

    assert result == "🌧️ 14°C · rain 40% · wind 18 km/h"
    assert "feels" not in result


def test_forecast_hour_line_returns_none_on_timeout():
    from brain.web_tools import forecast_hour_line

    at = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("UTC"))
    with patch(
        "brain.web_tools.requests.get",
        side_effect=[_city_geo_with_tz(), requests.exceptions.Timeout("slow")],
    ):
        result = forecast_hour_line("Lisbon, Portugal", at)

    assert result is None


def test_forecast_hour_line_returns_none_on_http_error():
    from brain.web_tools import forecast_hour_line

    broken = Mock(status_code=500)
    broken.raise_for_status = Mock(side_effect=requests.exceptions.HTTPError("boom"))
    at = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("UTC"))
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo_with_tz(), broken]):
        result = forecast_hour_line("Lisbon, Portugal", at)

    assert result is None


def test_forecast_hour_line_returns_none_when_geocode_finds_nothing():
    from brain.web_tools import forecast_hour_line

    at = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("UTC"))
    with patch("brain.web_tools.requests.get", return_value=_fake_geo_response([])):
        result = forecast_hour_line("Nowhereville", at)

    assert result is None


def test_forecast_hour_line_returns_none_when_hourly_keys_are_missing():
    from brain.web_tools import forecast_hour_line

    broken = Mock(status_code=200)
    broken.raise_for_status = Mock()
    broken.json.return_value = {"hourly": {"temperature_2m": [10.0] * 24}}
    at = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("UTC"))
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo_with_tz(), broken]):
        result = forecast_hour_line("Lisbon, Portugal", at)

    assert result is None


def test_forecast_hour_line_falls_back_to_the_configured_timezone_when_geocode_has_none():
    from brain.web_tools import forecast_hour_line

    temps = [10.0] * 24
    apparent = [10.0] * 24
    precip = [40] * 24
    wind = [18.0] * 24
    codes = [61] * 24
    temps[14], apparent[14] = 14.0, 12.0

    forecast = _fake_hour_forecast_response(temps, apparent, precip, wind, codes)
    # 10:00 UTC is 14:00 in Asia/Tbilisi (UTC+4, no DST) — the configured
    # fallback timezone, since _city_geo()'s result carries none of its own.
    at = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("UTC"))
    with (
        patch("brain.web_tools.requests.get", side_effect=[_city_geo(), forecast]),
        patch("brain.web_tools.resolve_timezone", return_value=ZoneInfo("Asia/Tbilisi")),
    ):
        result = forecast_hour_line("Lisbon, Portugal", at)

    assert result == "🌧️ 14°C (feels 12°C) · rain 40% · wind 18 km/h"


def test_forecast_hour_line_returns_none_when_a_field_array_is_shorter_than_time():
    from brain.web_tools import forecast_hour_line

    temps = [10.0] * 24
    apparent = [10.0] * 24
    precip = [0] * 24
    wind = [5.0] * 3  # malformed: shorter than every other array
    codes = [0] * 24
    forecast = _fake_hour_forecast_response(temps, apparent, precip, wind, codes)
    at = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("UTC"))
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo_with_tz(), forecast]):
        result = forecast_hour_line("Lisbon, Portugal", at)

    assert result is None


def test_forecast_hour_line_returns_none_when_the_temperature_is_null():
    from brain.web_tools import forecast_hour_line

    temps = [10.0] * 24
    temps[14] = None
    apparent = [10.0] * 24
    precip = [40] * 24
    wind = [18.0] * 24
    codes = [61] * 24
    forecast = _fake_hour_forecast_response(temps, apparent, precip, wind, codes)
    at = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("UTC"))
    with patch("brain.web_tools.requests.get", side_effect=[_city_geo_with_tz(), forecast]):
        result = forecast_hour_line("Lisbon, Portugal", at)

    assert result is None


def test_forecast_hour_line_uses_a_shorter_request_timeout():
    from brain.web_tools import forecast_hour_line

    forecast = _fake_hour_forecast_response(
        [10.0] * 24, [10.0] * 24, [0] * 24, [5.0] * 24, [0] * 24
    )
    at = datetime(2026, 9, 27, 10, 0, tzinfo=ZoneInfo("UTC"))
    with patch(
        "brain.web_tools.requests.get", side_effect=[_city_geo_with_tz(), forecast]
    ) as mock_get:
        forecast_hour_line("Lisbon, Portugal", at)

    for call in mock_get.call_args_list:
        assert call.kwargs["timeout"] == (3, 5)


def test_get_weather_still_uses_the_default_request_timeout():
    from brain.web_tools import get_weather

    geo = _fake_geo_response([{"latitude": 35.68, "longitude": 139.69, "name": "Tokyo"}])
    forecast = _fake_forecast_response(22.5, 1)
    with patch("brain.web_tools.requests.get", side_effect=[geo, forecast]) as mock_get:
        get_weather(Mock(), "Tokyo")

    for call in mock_get.call_args_list:
        assert call.kwargs["timeout"] == 15
