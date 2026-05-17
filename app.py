#!/usr/bin/env python3
"""
app.py

Flask app that reads calendars from calendars.txt and preferences from preferences.txt,
fetches .ics feeds for the configured display date, and serves a JSON snapshot at /api/events.

ICS handling is recurrence-aware and includes support for:
 - RRULE recurring events
 - RECURRENCE-ID modified/cancelled instances
 - EXDATE exclusions
 - RDATE additions
 - UID-based de-duplication
 - STATUS:CANCELLED filtering
 - optional all-day events via SHOW_ALL_DAY_EVENTS=true
 - timezone selection via TIMEZONE=Europe/London
 - display date selection via DISPLAY_DATE=today or DISPLAY_DATE=YYYY-MM-DD
 - HTTP retries and in-memory ETag/Last-Modified caching
 - last-known-good events when a calendar fetch temporarily fails
"""

from flask import Flask, jsonify, render_template
from datetime import datetime, date, time as datetime_time, timedelta
from icalendar import Calendar
import copy
import os
import re
import sys
import threading
import time
import traceback

import requests
from dateutil import parser as dtparser, tz
from dateutil.rrule import rruleset, rrulestr

try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover - only used on very old requests installs
    HTTPAdapter = None
    Retry = None

# === Configuration ===
CALENDAR_FILE = "calendars.txt"
PREFERENCES_FILE = "preferences.txt"
REFRESH_INTERVAL_SECONDS = 300  # 5 minutes
DEFAULT_TZ = "Europe/London"
HTTP_TIMEOUT_SECONDS = 20
HTTP_USER_AGENT = "calendar-dashboard/3.0"
APP_VERSION = "display-date-fixed-v2"

# Defaults (used when preferences are missing / invalid)
DEFAULT_WORK_START = 8
DEFAULT_WORK_END = 18
DEFAULT_FIT_TO_WINDOW = True
DEFAULT_HEADER_TEXT = "Calendar Dashboard"
DEFAULT_HEADER_FONT_SIZE = 20
DEFAULT_HEADER_FONT_FAMILY = "Arial, sans-serif"
DEFAULT_HEADER_COLOR = "#1a1a1a"

DEFAULT_EVENT_SHOW_TIMES = False
DEFAULT_EVENT_FONT_SIZE = 12
DEFAULT_EVENT_FONT_FAMILY = "Arial, sans-serif"
DEFAULT_EVENT_FONT_COLOR = "#000000"

DEFAULT_NOW_COLOR = "#e74c3c"
DEFAULT_NOW_THICKNESS = 2

DEFAULT_PAST_OVERLAY_COLOR = "#000000"
DEFAULT_PAST_OVERLAY_OPACITY = 0.12

# Keep original dashboard behaviour unless explicitly enabled.
DEFAULT_SHOW_ALL_DAY_EVENTS = False
DEFAULT_DISPLAY_DATE = "today"

# === App & cache globals ===
# Ensure template_folder works when frozen by PyInstaller.
if getattr(sys, "frozen", False):
    base_dir = sys._MEIPASS
else:
    base_dir = os.path.abspath(os.path.dirname(__file__))

template_dir = os.path.join(base_dir, "templates")
static_dir = os.path.join(base_dir, "static")
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)

# Read calendars.txt and preferences.txt from the app directory by default,
# not from whichever directory happened to be current when Python was launched.
# This prevents DISPLAY_DATE and other preferences from being silently ignored
# when the app is started from a different working directory.
CONFIG_DIR = os.environ.get("CALENDAR_DASHBOARD_CONFIG_DIR", base_dir)


def resolve_config_path(path):
    """Return an absolute path for a dashboard config file."""
    if os.path.isabs(path):
        return path
    return os.path.join(CONFIG_DIR, path)


def file_signature(path):
    """Return a cheap change signature for a config file, or None if missing."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (path, st.st_mtime_ns, st.st_size)


def config_files_signature():
    """Return a signature for files that should invalidate the snapshot cache."""
    return (
        file_signature(resolve_config_path(CALENDAR_FILE)),
        file_signature(resolve_config_path(PREFERENCES_FILE)),
    )


cache_lock = threading.Lock()
cached_snapshot = None  # will hold the JSON-like object to return from /api/events
cached_config_signature = None  # changes when calendars.txt/preferences.txt are edited or moved

http_cache_lock = threading.Lock()
http_response_cache = {}  # url -> {etag, last_modified, content}
http_session = None

last_good_lock = threading.Lock()
last_good_calendar_cache = {}  # url -> {events, refreshed_at}

# === Utility functions for reading files ===


def parse_input_line(line):
    """
    Parse a line of calendars.txt. Accepts:
      Label|URL|icon
      Label|URL
      URL
    Returns (label_or_none, url, icon_or_none).
    """
    parts = [p.strip() for p in re.split(r"\|", line, maxsplit=2)]
    if len(parts) == 3:
        label = parts[0] or None
        url = parts[1]
        icon = parts[2] or None
        return label, url, icon
    if len(parts) == 2:
        label = parts[0] or None
        url = parts[1]
        return label, url, None
    return None, parts[0], None


def read_calendars_file():
    """
    Returns list of (label_or_none, url, icon_or_none) entries.
    Ignores blank lines and lines starting with '#'.
    """
    path = resolve_config_path(CALENDAR_FILE)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                out.append(parse_input_line(line))
            except Exception:
                out.append((None, line, None))
    return out


def read_preferences_file():
    """Returns dict of preferences (raw string values)."""
    prefs = {}
    path = resolve_config_path(PREFERENCES_FILE)
    if not os.path.exists(path):
        return prefs
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            prefs[k.strip()] = v.strip()
    return prefs


# === Helpers to interpret preferences ===


def get_pref_bool(prefs, key, default):
    v = prefs.get(key)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_work_hours(prefs):
    try:
        ws = int(prefs.get("WORK_START_HOUR", DEFAULT_WORK_START))
        we = int(prefs.get("WORK_END_HOUR", DEFAULT_WORK_END))
    except Exception:
        return DEFAULT_WORK_START, DEFAULT_WORK_END
    if not (0 <= ws <= 23 and 0 <= we <= 23 and we > ws):
        return DEFAULT_WORK_START, DEFAULT_WORK_END
    return ws, we


def get_header_prefs(prefs):
    text = prefs.get("HEADER_TEXT", DEFAULT_HEADER_TEXT)
    try:
        size = int(prefs.get("HEADER_FONT_SIZE", DEFAULT_HEADER_FONT_SIZE))
    except Exception:
        size = DEFAULT_HEADER_FONT_SIZE
    fam = prefs.get("HEADER_FONT_FAMILY", DEFAULT_HEADER_FONT_FAMILY)
    color = prefs.get("HEADER_COLOR", DEFAULT_HEADER_COLOR)
    return {"text": text, "font_size": size, "font_family": fam, "color": color}


def get_event_prefs(prefs):
    show_times = get_pref_bool(prefs, "EVENT_SHOW_TIMES", DEFAULT_EVENT_SHOW_TIMES)
    try:
        size = int(prefs.get("EVENT_FONT_SIZE", DEFAULT_EVENT_FONT_SIZE))
    except Exception:
        size = DEFAULT_EVENT_FONT_SIZE
    fam = prefs.get("EVENT_FONT_FAMILY", DEFAULT_EVENT_FONT_FAMILY)
    color = prefs.get("EVENT_FONT_COLOR", DEFAULT_EVENT_FONT_COLOR)
    return {"show_times": show_times, "font_size": size, "font_family": fam, "color": color}


def get_now_prefs(prefs):
    color = prefs.get("NOW_LINE_COLOR", DEFAULT_NOW_COLOR)
    try:
        thickness = int(prefs.get("NOW_LINE_THICKNESS", DEFAULT_NOW_THICKNESS))
    except Exception:
        thickness = DEFAULT_NOW_THICKNESS
    return {"color": color, "thickness": thickness}


def get_past_overlay_prefs(prefs):
    color = prefs.get("PAST_OVERLAY_COLOR", DEFAULT_PAST_OVERLAY_COLOR)
    try:
        opacity = float(prefs.get("PAST_OVERLAY_OPACITY", DEFAULT_PAST_OVERLAY_OPACITY))
    except Exception:
        opacity = DEFAULT_PAST_OVERLAY_OPACITY
    if opacity < 0:
        opacity = 0.0
    if opacity > 1:
        opacity = 1.0
    return {"color": color, "opacity": opacity}


def get_calendar_colors(prefs):
    raw = prefs.get("CALENDAR_COLORS", "")
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    cleaned = []
    for p in parts:
        if p.startswith("#") and len(p) in (4, 7):
            cleaned.append(p)
        elif len(p) == 6 and all(c in "0123456789abcdefABCDEF" for c in p):
            cleaned.append("#" + p)
    return cleaned


def get_target_timezone(tz_name):
    target_tz = tz.gettz(tz_name)
    if target_tz is None:
        target_tz = tz.gettz(DEFAULT_TZ)
    if target_tz is None:
        raise ValueError("Could not load timezone")
    return target_tz

def get_display_date(prefs, target_tz):
    """Return (target_date, display_date_setting, warning).

    DISPLAY_DATE accepts:
      - today, now, or blank: use the current date in the configured timezone
      - YYYY-MM-DD: use that exact date

    Invalid values fall back to today and return a warning string for the API
    snapshot so configuration mistakes are visible without breaking the wallboard.
    """
    raw = str(prefs.get("DISPLAY_DATE", DEFAULT_DISPLAY_DATE)).strip()
    if not raw:
        raw = DEFAULT_DISPLAY_DATE

    lowered = raw.lower()
    if lowered in ("today", "now"):
        return datetime.now(target_tz).date(), "today", ""

    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
        return parsed, raw, ""
    except ValueError:
        fallback = datetime.now(target_tz).date()
        warning = (
            "Invalid DISPLAY_DATE={!r}; expected 'today' or YYYY-MM-DD. "
            "Falling back to today's date."
        ).format(raw)
        return fallback, raw, warning



# === HTTP fetching ===


def get_http_session():
    """Create a reusable requests session with retry/backoff support."""
    global http_session
    if http_session is not None:
        return http_session

    session = requests.Session()
    if HTTPAdapter is not None and Retry is not None:
        try:
            retry = Retry(
                total=3,
                connect=3,
                read=3,
                status=3,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "HEAD"]),
                raise_on_status=False,
            )
        except TypeError:
            # urllib3<1.26 used method_whitelist instead of allowed_methods.
            retry = Retry(
                total=3,
                connect=3,
                read=3,
                status=3,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                method_whitelist=frozenset(["GET", "HEAD"]),
                raise_on_status=False,
            )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

    http_session = session
    return http_session


def fetch_ics_content(ics_url):
    """
    Fetch ICS content with conditional HTTP caching.

    If the server returns 304 Not Modified, the cached bytes are reused. The
    cache is in-memory and lasts only for this process.
    """
    headers = {
        "User-Agent": HTTP_USER_AGENT,
        "Accept": "text/calendar, application/calendar, text/plain, */*",
    }

    with http_cache_lock:
        cached = http_response_cache.get(ics_url)
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]

    resp = get_http_session().get(ics_url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)

    if resp.status_code == 304:
        with http_cache_lock:
            cached = http_response_cache.get(ics_url)
            if cached and cached.get("content"):
                return cached["content"]
        raise RuntimeError("Server returned 304 but no cached ICS content is available")

    resp.raise_for_status()
    content = resp.content
    if not content:
        raise RuntimeError("Calendar feed was empty")

    with http_cache_lock:
        http_response_cache[ics_url] = {
            "etag": resp.headers.get("ETag"),
            "last_modified": resp.headers.get("Last-Modified"),
            "content": content,
        }

    return content


# === ICS parsing helpers ===


def get_raw_value(prop):
    """Return the decoded Python value from an icalendar property-like object."""
    if prop is None:
        return None
    return getattr(prop, "dt", prop)


def to_datetime(value, target_tz):
    """Convert a date/datetime or icalendar property to a timezone-aware datetime."""
    if value is None:
        return None

    raw = get_raw_value(value)

    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=target_tz)
        return raw.astimezone(target_tz)

    if isinstance(raw, date):
        return datetime.combine(raw, datetime_time.min).replace(tzinfo=target_tz)

    return None


def is_date_only(value):
    raw = get_raw_value(value)
    return isinstance(raw, date) and not isinstance(raw, datetime)


def event_status(comp):
    return str(comp.get("status") or "").strip().upper()


def is_cancelled(comp):
    return event_status(comp) == "CANCELLED"


def component_uid(comp):
    return str(comp.get("uid") or "").strip()


def fallback_component_identity(comp):
    summary = str(comp.get("summary") or "").strip()
    location = str(comp.get("location") or "").strip()
    return "NOUID:{}:{}".format(summary, location)


def component_identity(comp):
    return component_uid(comp) or fallback_component_identity(comp)


def normalize_key(value, target_tz):
    """Normalize recurrence keys so DTSTART and RECURRENCE-ID compare safely."""
    if value is None:
        return None

    raw = get_raw_value(value)
    if isinstance(raw, datetime):
        dt = to_datetime(raw, target_tz)
        if dt is None:
            return None
        return dt.replace(second=0, microsecond=0).isoformat()

    if isinstance(raw, date):
        return raw.isoformat()

    return str(raw)


def normalize_occurrence_key(dt_value, target_tz, all_day=False):
    if all_day:
        if isinstance(dt_value, datetime):
            return dt_value.astimezone(target_tz).date().isoformat()
        if isinstance(dt_value, date):
            return dt_value.isoformat()
    return normalize_key(dt_value, target_tz)


def get_end_datetime(comp, start_value, target_tz):
    """Return the event end as a timezone-aware datetime, or None."""
    start_dt = to_datetime(start_value, target_tz)
    if start_dt is None:
        return None

    end_value = comp.get("dtend")
    if end_value is not None:
        end_dt = to_datetime(end_value, target_tz)
        if end_dt is not None:
            return end_dt

    duration_value = comp.get("duration")
    if duration_value is not None:
        dur = get_raw_value(duration_value)
        if isinstance(dur, timedelta):
            return start_dt + dur

    return start_dt


def event_overlaps_datetimes(start_dt, end_dt, target_date, target_tz):
    """Return True when an event span overlaps the target date."""
    if start_dt is None:
        return False
    if end_dt is None:
        end_dt = start_dt

    day_start = datetime.combine(target_date, datetime_time.min).replace(tzinfo=target_tz)
    day_end = day_start + timedelta(days=1)
    return start_dt < day_end and end_dt > day_start


def event_overlaps_date(start_value, end_value, target_date, target_tz):
    start_dt = to_datetime(start_value, target_tz)
    end_dt = to_datetime(end_value, target_tz) if end_value is not None else start_dt
    return event_overlaps_datetimes(start_dt, end_dt, target_date, target_tz)


def format_span(start_dt, end_dt, target_date, target_tz, all_day=False):
    """Format the visible portion of an event on the target date."""
    if all_day:
        return "", ""

    day_start = datetime.combine(target_date, datetime_time.min).replace(tzinfo=target_tz)
    day_end = day_start + timedelta(days=1)
    display_start = max(start_dt, day_start)
    display_end = min(end_dt, day_end)

    if display_end >= day_end:
        end_str = "23:59"
    else:
        end_str = display_end.strftime("%H:%M")

    return display_start.strftime("%H:%M"), end_str


def rrule_to_text(rrule_prop):
    """Convert an RRULE property to a parsable RRULE string."""
    if rrule_prop is None:
        return None

    raw = get_raw_value(rrule_prop)
    if isinstance(raw, (list, tuple)) and raw:
        raw = raw[0]

    if hasattr(raw, "to_ical"):
        raw = raw.to_ical()

    if isinstance(raw, bytes):
        raw = raw.decode()

    if raw is None:
        return None

    text = str(raw).strip()
    if text.upper().startswith("RRULE:"):
        text = text[len("RRULE:") :]
    return text or None


def flatten_ical_datetime_values(prop):
    """Return decoded date/datetime values from EXDATE/RDATE-style properties."""
    if prop is None:
        return []

    values = []
    candidates = prop if isinstance(prop, (list, tuple, set)) else [prop]
    for item in candidates:
        if item is None:
            continue
        if hasattr(item, "dts"):
            for dt_item in item.dts:
                values.append(get_raw_value(dt_item))
            continue
        raw = get_raw_value(item)
        if isinstance(raw, (list, tuple, set)):
            values.extend(get_raw_value(v) for v in raw if v is not None)
        else:
            values.append(raw)
    return values


def coerce_recurrence_datetime(value, start_dt, target_tz, all_day=False):
    """Convert EXDATE/RDATE values into datetimes compatible with dateutil."""
    if isinstance(value, datetime):
        return to_datetime(value, target_tz)
    if isinstance(value, date):
        if all_day:
            return datetime.combine(value, datetime_time.min).replace(tzinfo=target_tz)
        return datetime.combine(value, start_dt.timetz().replace(tzinfo=None)).replace(tzinfo=target_tz)
    return to_datetime(value, target_tz)


def collect_override_keys(components, target_tz):
    """Collect RECURRENCE-ID values that are overridden by explicit VEVENTs."""
    keys_by_identity = {}
    for comp in components:
        if getattr(comp, "name", None) != "VEVENT":
            continue
        recur_id = get_raw_value(comp.get("recurrence-id"))
        if recur_id is None:
            continue
        identity = component_identity(comp)
        keys_by_identity.setdefault(identity, set()).add(normalize_key(recur_id, target_tz))
    return keys_by_identity


def build_recurrence_set(comp, start_dt, target_tz, all_day=False):
    """
    Build a dateutil rruleset for RRULE/RDATE/EXDATE.

    Returns None if the component has no recurrence-related properties.
    """
    rrule_text = rrule_to_text(comp.get("rrule"))
    rdate_values = flatten_ical_datetime_values(comp.get("rdate"))
    exdate_values = flatten_ical_datetime_values(comp.get("exdate"))

    if not rrule_text and not rdate_values and not exdate_values:
        return None

    rs = rruleset()
    added_any_dates = False

    if rrule_text:
        rs.rrule(rrulestr(rrule_text, dtstart=start_dt))
        added_any_dates = True
    else:
        # RDATE-only components still include their DTSTART instance.
        rs.rdate(start_dt)
        added_any_dates = True

    for rdate_value in rdate_values:
        rdate_dt = coerce_recurrence_datetime(rdate_value, start_dt, target_tz, all_day=all_day)
        if rdate_dt is not None:
            rs.rdate(rdate_dt)
            added_any_dates = True

    for exdate_value in exdate_values:
        exdate_dt = coerce_recurrence_datetime(exdate_value, start_dt, target_tz, all_day=all_day)
        if exdate_dt is not None:
            rs.exdate(exdate_dt)

    return rs if added_any_dates else None


def expand_recurrence_component(comp, target_date, target_tz, override_keys_by_identity):
    """Yield matching event spans for a recurring master VEVENT."""
    start_value = comp.get("dtstart")
    if start_value is None:
        return

    start_dt = to_datetime(start_value, target_tz)
    if start_dt is None:
        return

    end_dt = get_end_datetime(comp, start_value, target_tz)
    if end_dt is None:
        end_dt = start_dt

    duration = end_dt - start_dt
    all_day = is_date_only(start_value)
    identity = component_identity(comp)
    override_keys = override_keys_by_identity.get(identity, set())

    try:
        rs = build_recurrence_set(comp, start_dt, target_tz, all_day=all_day)
    except Exception:
        # If recurrence parsing fails, at least keep the master event if it overlaps.
        if event_overlaps_datetimes(start_dt, end_dt, target_date, target_tz):
            yield start_dt, end_dt, all_day, normalize_occurrence_key(start_dt, target_tz, all_day=all_day)
        return

    if rs is None:
        if event_overlaps_datetimes(start_dt, end_dt, target_date, target_tz):
            yield start_dt, end_dt, all_day, normalize_occurrence_key(start_dt, target_tz, all_day=all_day)
        return

    day_start = datetime.combine(target_date, datetime_time.min).replace(tzinfo=target_tz)
    day_end = day_start + timedelta(days=1)
    lookback = max(timedelta(days=1), duration if duration > timedelta(0) else timedelta(0))
    window_start = day_start - lookback - timedelta(minutes=1)

    for occ_start in rs.between(window_start, day_end, inc=True):
        occ_start = to_datetime(occ_start, target_tz)
        if occ_start is None:
            continue
        occ_end = occ_start + duration
        if not event_overlaps_datetimes(occ_start, occ_end, target_date, target_tz):
            continue
        occ_key = normalize_occurrence_key(occ_start, target_tz, all_day=all_day)
        if occ_key in override_keys:
            continue
        yield occ_start, occ_end, all_day, occ_key


def coerce_target_date(target_date, target_tz):
    """Accept a date, datetime, or YYYY-MM-DD string and return a date object."""
    if isinstance(target_date, datetime):
        if target_date.tzinfo is None:
            return target_date.replace(tzinfo=target_tz).date()
        return target_date.astimezone(target_tz).date()
    if isinstance(target_date, date):
        return target_date
    return dtparser.parse(str(target_date)).date()


def make_event_dict(comp, summary, start_dt, end_dt, target_date, target_tz, all_day, recurrence_key):
    start_str, end_str = format_span(start_dt, end_dt, target_date, target_tz, all_day=all_day)
    uid = component_uid(comp)
    return {
        "title": summary,
        "start": start_str,
        "end": end_str,
        "all_day": bool(all_day),
        "uid": uid,
        "start_iso": start_dt.isoformat() if start_dt else "",
        "end_iso": end_dt.isoformat() if end_dt else "",
        "status": event_status(comp) or "CONFIRMED",
        "recurrence_key": recurrence_key or "",
    }


def read_ics_events_for_date(ics_url, target_date, tz_name=DEFAULT_TZ, show_all_day_events=DEFAULT_SHOW_ALL_DAY_EVENTS):
    """
    Fetch the ICS file and return a list of event dicts.

    The dashboard still gets the original fields it expects: title, start, end.
    Extra fields are included for debugging and future UI improvements.
    """
    target_tz = get_target_timezone(tz_name)
    target_date = coerce_target_date(target_date, target_tz)

    cal = Calendar.from_ical(fetch_ics_content(ics_url))
    components = [comp for comp in cal.walk() if getattr(comp, "name", None) == "VEVENT"]
    override_keys_by_identity = collect_override_keys(components, target_tz)

    events = []
    seen = set()

    def add_event(comp, summary, start_dt, end_dt, all_day, recurrence_key):
        if all_day and not show_all_day_events:
            return
        uid = component_uid(comp)
        if uid:
            dedupe_key = ("uid", uid, recurrence_key or normalize_occurrence_key(start_dt, target_tz, all_day=all_day))
        else:
            dedupe_key = (
                "fallback",
                fallback_component_identity(comp),
                recurrence_key or normalize_occurrence_key(start_dt, target_tz, all_day=all_day),
            )
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        events.append(make_event_dict(comp, summary, start_dt, end_dt, target_date, target_tz, all_day, recurrence_key))

    for comp in components:
        if is_cancelled(comp):
            # Cancelled RECURRENCE-ID components are already represented in override_keys,
            # so the matching master occurrence is suppressed above.
            continue

        summary = str(comp.get("summary") or "").strip()
        start_value = comp.get("dtstart")
        if start_value is None:
            continue

        recur_id = comp.get("recurrence-id")
        has_recurrence = any(comp.get(name) is not None for name in ("rrule", "rdate", "exdate"))

        if recur_id is not None:
            # Explicit exception instance. DTSTART may differ from RECURRENCE-ID.
            start_value_for_instance = start_value or recur_id
            start_dt = to_datetime(start_value_for_instance, target_tz)
            if start_dt is None:
                continue
            end_dt = get_end_datetime(comp, start_value_for_instance, target_tz) or start_dt
            if not event_overlaps_datetimes(start_dt, end_dt, target_date, target_tz):
                continue
            all_day = is_date_only(start_value_for_instance)
            add_event(comp, summary, start_dt, end_dt, all_day, normalize_key(recur_id, target_tz))
            continue

        if has_recurrence:
            for occ_start, occ_end, all_day, occ_key in expand_recurrence_component(
                comp, target_date, target_tz, override_keys_by_identity
            ):
                add_event(comp, summary, occ_start, occ_end, all_day, occ_key)
            continue

        # Plain one-off event.
        start_dt = to_datetime(start_value, target_tz)
        if start_dt is None:
            continue
        end_dt = get_end_datetime(comp, start_value, target_tz) or start_dt
        if not event_overlaps_datetimes(start_dt, end_dt, target_date, target_tz):
            continue
        all_day = is_date_only(start_value)
        add_event(comp, summary, start_dt, end_dt, all_day, normalize_occurrence_key(start_dt, target_tz, all_day=all_day))

    events.sort(
        key=lambda ev: (
            1 if not ev.get("start") else 0,
            datetime.strptime(ev["start"], "%H:%M").time() if ev.get("start") else datetime_time.min,
            ev.get("title", "").lower(),
        )
    )
    return events


# === Build snapshot ===


def build_snapshot_for_display_date():
    """Build the JSON-like snapshot for the configured display date."""
    prefs = read_preferences_file()
    tz_name = prefs.get("TIMEZONE", DEFAULT_TZ)
    target_tz = get_target_timezone(tz_name)
    display_date, display_date_setting, display_date_warning = get_display_date(prefs, target_tz)
    actual_today = datetime.now(target_tz).date()
    show_all_day_events = get_pref_bool(prefs, "SHOW_ALL_DAY_EVENTS", DEFAULT_SHOW_ALL_DAY_EVENTS)

    work_start, work_end = get_work_hours(prefs)
    fit_to_window = get_pref_bool(prefs, "FIT_TO_WINDOW", DEFAULT_FIT_TO_WINDOW)
    header = get_header_prefs(prefs)
    event_prefs = get_event_prefs(prefs)
    now_prefs = get_now_prefs(prefs)
    past_overlay = get_past_overlay_prefs(prefs)
    calendar_colors = get_calendar_colors(prefs)
    logo_path = prefs.get("LOGO_PATH", "")

    snapshot = {
        "work_start_hour": work_start,
        "work_end_hour": work_end,
        "fit_to_window": fit_to_window,
        "header": header,
        "event_prefs": event_prefs,
        "now_prefs": now_prefs,
        "past_overlay": past_overlay,
        "logo_path": logo_path,
        "calendar_colors": calendar_colors,
        "date": display_date.strftime("%Y-%m-%d"),
        "display_date": display_date.strftime("%Y-%m-%d"),
        "display_date_setting": display_date_setting,
        "display_date_warning": display_date_warning,
        "actual_today": actual_today.strftime("%Y-%m-%d"),
        "is_today": display_date == actual_today,
        "timezone": tz_name,
        "show_all_day_events": show_all_day_events,
        "app_version": APP_VERSION,
        "config_paths": {
            "calendars_file": resolve_config_path(CALENDAR_FILE),
            "preferences_file": resolve_config_path(PREFERENCES_FILE),
        },
        "calendars": [],
    }

    calendars = read_calendars_file()
    for label, url, icon in calendars:
        calendar_entry = {
            "label": label or url,
            "url": url,
            "icon": icon or "",
            "events": [],
        }
        try:
            events = read_ics_events_for_date(
                url,
                display_date,
                tz_name=tz_name,
                show_all_day_events=show_all_day_events,
            )
            calendar_entry["events"] = events
            calendar_entry["status"] = "ok"
            calendar_entry["last_successful_refresh"] = datetime.now(target_tz).isoformat()
            with last_good_lock:
                last_good_calendar_cache[url] = {
                    "events": copy.deepcopy(events),
                    "refreshed_at": calendar_entry["last_successful_refresh"],
                }
        except Exception as e:
            err_text = f"{type(e).__name__}: {str(e)}"
            print(f"[app.py] Error fetching {url}: {err_text}")
            traceback.print_exc()
            with last_good_lock:
                last_good = copy.deepcopy(last_good_calendar_cache.get(url))
            if last_good:
                calendar_entry["events"] = last_good.get("events", [])
                calendar_entry["status"] = "stale"
                calendar_entry["warning"] = "Using last successful calendar data because refresh failed: " + err_text
                calendar_entry["last_successful_refresh"] = last_good.get("refreshed_at", "")
            else:
                calendar_entry["events"] = [{"title": "ERROR: " + err_text, "start": "", "end": "", "all_day": False}]
                calendar_entry["status"] = "error"
                calendar_entry["error"] = err_text

        snapshot["calendars"].append(calendar_entry)

    return snapshot


# === Cache refresher ===


def build_snapshot_for_today():
    """Backward-compatible wrapper for older imports/tests."""
    return build_snapshot_for_display_date()


def refresh_all_calendars():
    global cached_snapshot, cached_config_signature
    try:
        print("[app.py] Refreshing calendars...")
        new_snapshot = build_snapshot_for_display_date()
        new_signature = config_files_signature()
        with cache_lock:
            cached_snapshot = new_snapshot
            cached_config_signature = new_signature
        print("[app.py] Refresh complete.")
    except Exception as ex:
        print("[app.py] Exception in refresh_all_calendars:", ex)
        traceback.print_exc()


def refresh_loop():
    try:
        refresh_all_calendars()
    except Exception:
        pass
    while True:
        time.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            refresh_all_calendars()
        except Exception:
            print("[app.py] Exception in scheduled refresh:")
            traceback.print_exc()


def start_background_refresher():
    t = threading.Thread(target=refresh_loop, name="calendar-refresher", daemon=True)
    t.start()
    print("[app.py] Background refresher started (interval {}s).".format(REFRESH_INTERVAL_SECONDS))
    print("[app.py] Version: {}".format(APP_VERSION))
    print("[app.py] Config dir: {}".format(CONFIG_DIR))
    print("[app.py] Preferences file: {}".format(resolve_config_path(PREFERENCES_FILE)))
    print("[app.py] Calendars file: {}".format(resolve_config_path(CALENDAR_FILE)))


# === Flask routes ===


@app.route("/")
@app.route("/calendars.html")
def index():
    return render_template("calendars.html")


@app.route("/api/events")
def api_events():
    global cached_snapshot, cached_config_signature
    current_signature = config_files_signature()
    with cache_lock:
        snap = cached_snapshot
        snap_signature = cached_config_signature
    if snap is None or snap_signature != current_signature:
        try:
            if snap is None:
                print("[app.py] Cache empty on request - building snapshot on-demand.")
            else:
                print("[app.py] Config files changed - rebuilding snapshot on-demand.")
            snap = build_snapshot_for_display_date()
            with cache_lock:
                cached_snapshot = snap
                cached_config_signature = current_signature
        except Exception as ex:
            print("[app.py] Failed to build snapshot on-demand:", ex)
            traceback.print_exc()
            response = jsonify({
                "error": "Failed to build calendar snapshot",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "display_date": datetime.now().strftime("%Y-%m-%d"),
                "calendars": [],
                "app_version": APP_VERSION,
            })
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            return response, 500
    response = jsonify(snap)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


if __name__ == "__main__":
    start_background_refresher()
    app.run(debug=True, host="0.0.0.0", port=5000)
