import logging
import re
from collections import Counter
from datetime import date, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
import yt_dlp

from brain.memory import get_all_facts
from config import resolve_exa_api_key, resolve_timezone

logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 3000
_REQUEST_TIMEOUT_SECONDS = 15


def _truncate(text: str, max_chars: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated]"


def _looks_like_url(value: str) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def read_webpage(url: str) -> str:
    if not _looks_like_url(url):
        return "That doesn't look like a valid URL."
    try:
        response = requests.get(
            f"https://r.jina.ai/{url}", timeout=_REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.warning("read_webpage failed for %s: %s", url, exc)
        return "I couldn't reach that page right now."
    return _truncate(response.text.strip())


READ_WEBPAGE_TOOL = {
    "name": "read_webpage",
    "description": (
        "Fetch and read the text content of a web page the user gives you "
        "a URL for, or asks you to look up online. Use this when the user "
        "shares a link and asks about it, or asks you to check what's on "
        "a specific page."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The full URL of the page to read"},
        },
        "required": ["url"],
    },
}


_OWNER_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def get_github_repo(owner_repo: str) -> str:
    cleaned = owner_repo.strip()
    if cleaned.startswith("https://github.com/"):
        cleaned = cleaned[len("https://github.com/"):]
    cleaned = cleaned.rstrip("/")
    if not _OWNER_REPO_RE.match(cleaned):
        return "That doesn't look like a valid repo — can you give it as 'owner/repo'?"

    try:
        response = requests.get(
            f"https://api.github.com/repos/{cleaned}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        logger.warning("get_github_repo failed for %s: %s", cleaned, exc)
        return "I couldn't reach GitHub right now."

    if response.status_code == 404:
        return f"I couldn't find a repo at '{cleaned}'."
    try:
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        logger.warning("get_github_repo failed for %s: %s", cleaned, exc)
        return "I couldn't reach GitHub right now."
    description = data.get("description") or "no description"
    stars = data.get("stargazers_count", 0)
    language = data.get("language") or "unknown language"
    updated = data.get("pushed_at", "unknown")
    return (
        f"{data.get('full_name', cleaned)}: {description}\n"
        f"Stars: {stars} | Language: {language} | Last pushed: {updated}"
    )


GET_GITHUB_REPO_TOOL = {
    "name": "get_github_repo",
    "description": (
        "Look up basic public information about a GitHub repository — "
        "description, stars, primary language, last activity — when the "
        "user asks about a specific repo."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "owner_repo": {
                "type": "string",
                "description": "The repo as 'owner/repo', e.g. 'torvalds/linux'",
            },
        },
        "required": ["owner_repo"],
    },
}


def read_rss_feed(url: str) -> str:
    if not _looks_like_url(url):
        return "That doesn't look like a valid URL."
    try:
        response = requests.get(url, timeout=_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.warning("read_rss_feed failed to fetch %s: %s", url, exc)
        return "I couldn't read that feed right now."
    feed = feedparser.parse(response.content)
    if feed.bozo and not feed.entries:
        logger.warning("read_rss_feed failed to parse %s", url)
        return "I couldn't read that feed right now."
    if not feed.entries:
        return "That feed doesn't have any entries right now."

    lines = []
    for entry in feed.entries[:5]:
        title = entry.get("title", "Untitled")
        summary = entry.get("summary", "")
        lines.append(f"- {title}: {summary}")
    return _truncate("\n".join(lines))


READ_RSS_FEED_TOOL = {
    "name": "read_rss_feed",
    "description": (
        "Fetch and summarize the most recent entries from an RSS or Atom "
        "feed URL the user gives you."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The full URL of the RSS/Atom feed"},
        },
        "required": ["url"],
    },
}


_WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    71: "slight snow", 73: "moderate snow", 75: "heavy snow",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    95: "thunderstorm",
}


class _GeocodeNotFound(Exception):
    """No place matched the given location string."""


# Location string -> (name, latitude, longitude, timezone_or_None). A small
# in-process cache: the same home location is looked up on every list post
# and every fitness ping, and geocoding it fresh every time would just be
# wasted latency for a result that never changes.
_geocode_cache: dict[str, tuple[str, float, float, str | None]] = {}


def _geocode(
    location: str, timeout: float | tuple = _REQUEST_TIMEOUT_SECONDS,
) -> tuple[str, float, float, str | None]:
    """Resolve `location` via Open-Meteo's geocoding API, cached in-process.

    Raises _GeocodeNotFound when no place matches; propagates
    RequestException/ValueError/KeyError for the caller to treat as a
    generic failure, same as any other step of a weather lookup. Only a
    successful lookup that also carries a timezone is cached — a transient
    failure must not get stuck, and neither should a degraded result
    missing the timezone forecast_hour_line needs (a later lookup might
    still get a better answer).
    """
    if location in _geocode_cache:
        return _geocode_cache[location]
    geo_response = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": location, "count": 1},
        timeout=timeout,
    )
    geo_response.raise_for_status()
    results = geo_response.json().get("results")
    if not results:
        raise _GeocodeNotFound(location)
    place = results[0]
    result = (place["name"], place["latitude"], place["longitude"], place.get("timezone"))
    if result[3]:
        _geocode_cache[location] = result
    return result


def get_weather(conn, location: str | None = None) -> str:
    if not location:
        location = get_all_facts(conn).get("location")
    if not location:
        return "Which city would you like the weather for?"

    try:
        place_name, latitude, longitude, _timezone = _geocode(location)
        forecast_response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,weather_code",
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        forecast_response.raise_for_status()
        current = forecast_response.json().get("current", {})
    except _GeocodeNotFound:
        return f"I couldn't find a place called '{location}'."
    except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
        logger.warning("get_weather failed for %s: %s", location, exc)
        return "I couldn't reach the weather service right now."

    temperature = current.get("temperature_2m")
    condition = _WEATHER_CODES.get(current.get("weather_code"), "unknown conditions")
    return f"{place_name}: {temperature}°C, {condition}."


# --- day/hour forecast lines (morning list + fitness pings) --------------

_DAY_EMOJI_BY_CATEGORY = {
    "clear": "☀️",
    "cloudy": "⛅",
    "overcast": "☁️",
    "fog": "🌫️",
    "rain": "🌧️",
    "snow": "🌨️",
    "thunderstorm": "⛈️",
}
_MIXED_SUN_RAIN_EMOJI = "🌦️"
_CATEGORY_BY_WEATHER_CODE = {
    0: "clear",
    1: "cloudy", 2: "cloudy",
    3: "overcast",
    45: "fog", 48: "fog",
    51: "rain", 53: "rain", 55: "rain",
    56: "rain", 57: "rain",  # freezing drizzle
    61: "rain", 63: "rain", 65: "rain",
    66: "rain", 67: "rain",  # freezing rain
    80: "rain", 81: "rain", 82: "rain",
    71: "snow", 73: "snow", 75: "snow",
    77: "snow",  # snow grains
    85: "snow", 86: "snow",  # snow showers
    95: "thunderstorm",
    96: "thunderstorm", 99: "thunderstorm",  # thunderstorm with hail
}
# Least to most severe — used only to break a frequency tie.
_CATEGORY_SEVERITY = ["clear", "cloudy", "overcast", "fog", "rain", "snow", "thunderstorm"]
_RAIN_PROBABILITY_THRESHOLD = 50
# Only hours in this window count towards the day's emoji — a clear
# overnight stretch shouldn't earn a day a "sunny" mixed emoji, and no one
# reads the list at 3am anyway.
_DAYTIME_START_HOUR = 7
_DAYTIME_END_HOUR = 21
# A shorter timeout than the other web tools: the weather line is a
# nice-to-have shown alongside a reminder ping/list that must go out
# regardless, so a slow weather service should fail fast rather than eat
# into the caller's own ~6s hard cap.
_WEATHER_REQUEST_TIMEOUT = (3, 5)  # (connect, read) seconds


def _day_emoji(times: list[str], codes: list[int], has_rain: bool) -> str:
    """Picks the whole-day emoji from DAYTIME (07:00-21:00) weather codes —
    night-only codes don't affect what looks right during the hours someone
    actually reads the list. `has_rain` (from precipitation_probability, the
    same signal the rain-likely text is built from) gates whether the "rain"
    category may be shown at all: weather_code and precipitation probability
    don't always agree, and a rain-flavored emoji (🌦️/🌧️) next to
    "no rain expected" text would be a visible contradiction — so when
    `has_rain` is False, a code-driven "rain" reading is dropped in favor of
    the next best-fitting category rather than shown anyway."""
    daytime_codes = [
        c for t, c in zip(times, codes)
        if _DAYTIME_START_HOUR <= int(t[11:13]) <= _DAYTIME_END_HOUR
    ]
    codes_to_use = daytime_codes or codes
    categories = [_CATEGORY_BY_WEATHER_CODE[c] for c in codes_to_use if c in _CATEGORY_BY_WEATHER_CODE]
    if not has_rain:
        categories = [cat for cat in categories if cat != "rain"]
    if not categories:
        return _DAY_EMOJI_BY_CATEGORY["cloudy"]
    has_sun = any(cat in ("clear", "cloudy") for cat in categories)
    has_rain_category = "rain" in categories
    if has_sun and has_rain_category:
        return _MIXED_SUN_RAIN_EMOJI
    counts = Counter(categories)
    top_count = max(counts.values())
    candidates = [cat for cat, n in counts.items() if n == top_count]
    candidates.sort(key=_CATEGORY_SEVERITY.index, reverse=True)
    return _DAY_EMOJI_BY_CATEGORY[candidates[0]]


def _hour_emoji(code: int) -> str:
    """The emoji for a single hour's own weather code — used by
    forecast_hour_line, where (unlike the whole-day line) there's no
    separate probability-driven text to stay consistent with, so the code
    is trusted directly."""
    category = _CATEGORY_BY_WEATHER_CODE.get(code)
    return _DAY_EMOJI_BY_CATEGORY.get(category, _DAY_EMOJI_BY_CATEGORY["cloudy"])


def _rain_hour_ranges(hours: list[int]) -> list[tuple[int, int]]:
    """Merge a list of hour-of-day ints into (start, end) ranges of
    consecutive hours, end being one past the last rainy hour (may be 24,
    meaning "runs to midnight")."""
    ranges: list[tuple[int, int]] = []
    start = prev = None
    for h in sorted(set(hours)):
        if start is None:
            start = h
        elif h != prev + 1:
            ranges.append((start, prev + 1))
            start = h
        prev = h
    if start is not None:
        ranges.append((start, prev + 1))
    return ranges


def _format_rain_part(ranges: list[tuple[int, int]]) -> str:
    if not ranges:
        return "no rain expected"
    formatted = [
        f"from {s:02d}:00" if e >= 24 else f"{s:02d}:00–{e:02d}:00" for s, e in ranges
    ]
    suffix = "…" if len(formatted) > 2 else ""
    return "rain likely " + ", ".join(formatted[:2]) + suffix


def forecast_day_line(location: str, day: date, tz: ZoneInfo) -> str | None:
    """One line summarizing `day`'s whole-day forecast for `location`, or
    None on any failure — this is a nice-to-have on the morning list and
    must never raise. The text always says "today", since it's meant to be
    fetched once and posted on the day it describes (see brain.daily_list's
    _sync_daily_list, which fetches it only for a new day's first post)."""
    try:
        place_name, latitude, longitude, _timezone = _geocode(
            location, timeout=_WEATHER_REQUEST_TIMEOUT
        )
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "hourly": "temperature_2m,precipitation_probability,weather_code",
                "timezone": tz.key,
                "start_date": day.isoformat(),
                "end_date": day.isoformat(),
            },
            timeout=_WEATHER_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        hourly = response.json()["hourly"]
        times = hourly["time"]
        temps = hourly["temperature_2m"]
        precip = hourly["precipitation_probability"]
        codes = hourly["weather_code"]
        if not times or not temps:
            raise ValueError("empty hourly forecast")

        real_temps = [t for t in temps if t is not None]
        min_t = round(min(real_temps))
        max_t = round(max(real_temps))
        temp_part = f"{min_t}°C" if min_t == max_t else f"{min_t}–{max_t}°C"
        rainy_hours = [
            int(t[11:13]) for t, p in zip(times, precip)
            if p is not None and p >= _RAIN_PROBABILITY_THRESHOLD
        ]
        rain_part = _format_rain_part(_rain_hour_ranges(rainy_hours))
        emoji = _day_emoji(times, codes, has_rain=bool(rainy_hours))
        result = f"{emoji} {place_name} today: {temp_part} · {rain_part}"
    except (
        requests.exceptions.RequestException, ValueError, KeyError, TypeError,
        IndexError, _GeocodeNotFound,
    ) as exc:
        logger.warning("forecast_day_line failed for %s: %s", location, exc)
        return None
    return result


def forecast_hour_line(location: str, at: datetime) -> str | None:
    """One line summarizing the forecast for the local hour containing
    `at`, or None on any failure — this is a nice-to-have on fitness pings
    and must never raise. `at` may carry any tzinfo (e.g. UTC from the DB);
    it's converted to the location's own timezone (from geocoding, falling
    back to the app's configured timezone if geocoding didn't return one)
    to determine which local hour to report."""
    try:
        _place_name, latitude, longitude, timezone_name = _geocode(
            location, timeout=_WEATHER_REQUEST_TIMEOUT
        )
        if not timezone_name:
            timezone_name = resolve_timezone().key
        local_at = at.astimezone(ZoneInfo(timezone_name))
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "hourly": (
                    "temperature_2m,apparent_temperature,"
                    "precipitation_probability,wind_speed_10m,weather_code"
                ),
                "timezone": timezone_name,
                "start_date": local_at.date().isoformat(),
                "end_date": local_at.date().isoformat(),
            },
            timeout=_WEATHER_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        hourly = response.json()["hourly"]
        times = hourly["time"]
        index = next(i for i, t in enumerate(times) if int(t[11:13]) == local_at.hour)
        temperature = hourly["temperature_2m"][index]
        feels_like = hourly["apparent_temperature"][index]
        precipitation = hourly["precipitation_probability"][index]
        wind = hourly["wind_speed_10m"][index]
        code = hourly["weather_code"][index]

        t = round(temperature)
        feels = round(feels_like)
        feels_part = f" (feels {feels}°C)" if feels != t else ""
        emoji = _hour_emoji(code)
        result = f"{emoji} {t}°C{feels_part} · rain {round(precipitation)}% · wind {round(wind)} km/h"
    except (
        requests.exceptions.RequestException, ValueError, KeyError, TypeError,
        IndexError, StopIteration, _GeocodeNotFound,
    ) as exc:
        logger.warning("forecast_hour_line failed for %s: %s", location, exc)
        return None
    return result


GET_WEATHER_TOOL = {
    "name": "get_weather",
    "description": (
        "Get the current weather for a city. If the user doesn't name a "
        "city, leave location empty — this falls back to their remembered "
        "home location automatically."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "City name, optional — omit to use the remembered default",
            },
        },
        "required": [],
    },
}


def _select_caption_url(info: dict) -> str | None:
    captions = info.get("automatic_captions") or {}
    tracks = captions.get("en") or next(iter(captions.values()), None)
    if not tracks:
        return None
    for track in tracks:
        if track.get("ext") == "json3":
            return track.get("url")
    return tracks[0].get("url")


def _fetch_transcript_text(caption_url: str) -> str:
    response = requests.get(caption_url, timeout=_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()
    pieces = [
        seg.get("utf8", "")
        for event in data.get("events", [])
        for seg in event.get("segs", [])
    ]
    return "".join(pieces).strip()


def get_youtube_info(url: str) -> str:
    if not _looks_like_url(url):
        return "That doesn't look like a valid URL."

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.YoutubeDLError as exc:
        logger.warning("get_youtube_info failed for %s: %s", url, exc)
        return "I couldn't fetch that video's info right now."

    if not info:
        return "I couldn't fetch that video's info right now."

    title = info.get("title", "Unknown title")
    uploader = info.get("uploader", "Unknown uploader")
    duration = info.get("duration")
    duration_str = f"{duration // 60}m {duration % 60}s" if duration else "unknown length"
    summary = f"Title: {title}\nUploader: {uploader}\nDuration: {duration_str}"

    caption_url = _select_caption_url(info)
    transcript = ""
    if caption_url:
        try:
            transcript = _fetch_transcript_text(caption_url)
        except requests.exceptions.RequestException as exc:
            logger.warning("get_youtube_info transcript fetch failed for %s: %s", url, exc)

    if transcript:
        summary += f"\n\nTranscript excerpt:\n{_truncate(transcript)}"
    else:
        summary += "\n\n(No transcript/captions available for this video.)"
    return summary


GET_YOUTUBE_INFO_TOOL = {
    "name": "get_youtube_info",
    "description": (
        "Look up a YouTube video's title, uploader, duration, and "
        "transcript when the user shares a YouTube link or asks what a "
        "specific video is about."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The full YouTube video URL"},
        },
        "required": ["url"],
    },
}


def web_search(query: str) -> str:
    api_key = resolve_exa_api_key()
    if not api_key:
        return "Web search isn't configured right now."

    try:
        response = requests.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={
                "query": query,
                "numResults": 5,
                "type": "auto",
                "contents": {"text": {"maxCharacters": 500}},
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except (requests.exceptions.RequestException, ValueError) as exc:
        logger.warning("web_search failed for %r: %s", query, exc)
        return "I couldn't search the web right now."

    if not results:
        return "I didn't find anything for that search."
    lines = []
    for r in results:
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        snippet = (r.get("text") or "").strip()
        if snippet:
            lines.append(f"- {title} ({url}): {snippet}")
        else:
            lines.append(f"- {title}: {url}")
    return _truncate("\n".join(lines))


WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": (
        "Search the web for current information not covered by known "
        "facts or notes — news, general knowledge, anything requiring a "
        "broader search rather than one specific page."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
        },
        "required": ["query"],
    },
}
