#!/usr/bin/env python3
#!/usr/bin/env python3
"""
app.py

Flask app that reads calendars from calendars.txt and preferences from preferences.txt,
fetches .ics feeds for today's date, and serves a JSON snapshot at /api/events.

This version supports:
 - calendars.txt lines of the form: Label|URL|icon_or_filename (icon optional)
 - preferences including LOGO_PATH, PAST_OVERLAY_COLOR, PAST_OVERLAY_OPACITY
 - background refresher thread that updates every REFRESH_INTERVAL_SECONDS
"""

from flask import Flask, jsonify, render_template
from datetime import datetime, date, timedelta
from icalendar import Calendar
import requests
import re
import os
import threading
import time
import traceback

# === Configuration ===
CALENDAR_FILE = "calendars.txt"
PREFERENCES_FILE = "preferences.txt"
REFRESH_INTERVAL_SECONDS = 300  # 5 minutes

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

# === App & cache globals ===
# Ensure template_folder works when frozen by PyInstaller
import sys
if getattr(sys, 'frozen', False):
    base_dir = sys._MEIPASS
else:
    base_dir = os.path.abspath(os.path.dirname(__file__))

template_dir = os.path.join(base_dir, "templates")
static_dir = os.path.join(base_dir, "static")
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)

cache_lock = threading.Lock()
cached_snapshot = None  # will hold the JSON-like object to return from /api/events

# === Utility functions for reading files ===

def parse_input_line(line):
    """
    Parse a line of calendars.txt. Accepts:
      Label|URL|icon
      Label|URL
      URL
    Returns (label_or_none, url, icon_or_none).
    """
    # split on first two separators
    parts = [p.strip() for p in re.split(r'\|', line, maxsplit=2)]
    if len(parts) == 3:
        label = parts[0] or None
        url = parts[1]
        icon = parts[2] or None
        return label, url, icon
    if len(parts) == 2:
        label = parts[0] or None
        url = parts[1]
        return label, url, None
    # single part -> URL (no label)
    return None, parts[0], None

def read_calendars_file():
    """
    Returns list of (label_or_none, url, icon_or_none) entries.
    Ignores blank lines and lines starting with '#'.
    """
    if not os.path.exists(CALENDAR_FILE):
        return []
    out = []
    with open(CALENDAR_FILE, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                parsed = parse_input_line(line)
                out.append(parsed)
            except Exception:
                # fallback to old behaviour
                out.append((None, line, None))
    return out

def read_preferences_file():
    """
    Returns dict of preferences (raw string values).
    """
    prefs = {}
    if not os.path.exists(PREFERENCES_FILE):
        return prefs
    with open(PREFERENCES_FILE, "r", encoding="utf-8") as f:
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
    # clamp
    if opacity < 0: opacity = 0.0
    if opacity > 1: opacity = 1.0
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
        else:
            if len(p) == 6 and all(c in "0123456789abcdefABCDEF" for c in p):
                cleaned.append("#" + p)
    return cleaned

# === ICS fetching & parsing ===

def _flatten_ical_datetime_values(prop):
    """Return a list of decoded datetime/date values from an icalendar property."""
    if prop is None:
        return []

    # icalendar may return a single vDDDTypes, a vDDDLists container, or a plain list.
    values = []
    candidates = prop if isinstance(prop, (list, tuple, set)) else [prop]
    for item in candidates:
        if item is None:
            continue
        if hasattr(item, "dts"):
            for dt_item in item.dts:
                values.append(getattr(dt_item, "dt", dt_item))
            continue
        values.append(getattr(item, "dt", item))
    return values


def _event_occurs_on_date(comp, target_date):
    """
    Return True when a VEVENT should be included for target_date.

    This handles both plain VEVENTs and recurring masters with RRULE plus
    exception instances provided via RECURRENCE-ID.
    """
    dtstart = comp.get("dtstart")
    if not dtstart:
        return False

    start = dtstart.dt
    dtend = comp.get("dtend")
    end = dtend.dt if dtend else start

    # Skip all-day events for compatibility with the original dashboard logic.
    from datetime import date as _date, datetime as _datetime
    if isinstance(start, _date) and not isinstance(start, _datetime):
        return False

    # Fast path for non-recurring events and exception instances.
    try:
        if start.date() == target_date:
            return True
        if isinstance(end, _datetime) and start.date() <= target_date <= end.date():
            return True
    except Exception:
        pass

    # Recurring master: expand occurrences and check whether one lands on the target day.
    rrule_prop = comp.get("rrule")
    if rrule_prop:
        try:
            from dateutil.rrule import rruleset, rrulestr

            rule_text = str(rrule_prop)
            if not rule_text.upper().startswith("RRULE:"):
                rule_text = "RRULE:" + rule_text

            rs = rruleset()
            rs.rrule(rrulestr(rule_text, dtstart=start))

            for exdate in _flatten_ical_datetime_values(comp.get("exdate")):
                rs.exdate(exdate)
            for rdate in _flatten_ical_datetime_values(comp.get("rdate")):
                rs.rdate(rdate)

            day_start = datetime.combine(target_date, datetime.min.time())
            if isinstance(start, _datetime) and start.tzinfo is not None:
                day_start = day_start.replace(tzinfo=start.tzinfo)
            day_end = day_start + timedelta(days=1)
            if rs.between(day_start, day_end, inc=True):
                return True
        except Exception:
            # Fall back to the direct date checks above.
            pass

    return False

def read_ics_events_for_date(ics_url, target_date):
    """
    Fetch the ICS file and return a list of dicts: {title, start, end}
    Times are strings "HH:MM".
    If a feed fails, this function raises Exception.
    """
    resp = requests.get(ics_url, timeout=20)
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.content)
    events = []

    for comp in cal.walk():
        if comp.name != "VEVENT":
            continue

        if not _event_occurs_on_date(comp, target_date):
            continue

        summary = str(comp.get("summary") or "")
        dtstart = comp.get("dtstart")
        dtend = comp.get("dtend")
        if not dtstart:
            continue

        start = dtstart.dt
        end = dtend.dt if dtend else start

        # Skip all-day events (date objects without time) for compatibility with the
        # original dashboard behaviour.
        from datetime import date as _date, datetime as _datetime
        if isinstance(start, _date) and not isinstance(start, _datetime):
            continue

        try:
            events.append({
                "title": summary,
                "start": start.strftime("%H:%M"),
                "end": end.strftime("%H:%M"),
            })
        except Exception:
            continue

    # sort by start
    events.sort(key=lambda e: e.get("start") or "99:99")
    return events

# === Build snapshot ===

def build_snapshot_for_today():
    """
    Build the JSON-like snapshot for today's date based on calendars.txt and preferences.
    """
    today = datetime.now().date()
    prefs = read_preferences_file()
    work_start, work_end = get_work_hours(prefs)
    fit_to_window = get_pref_bool(prefs, "FIT_TO_WINDOW", DEFAULT_FIT_TO_WINDOW)
    header = get_header_prefs(prefs)
    event_prefs = get_event_prefs(prefs)
    now_prefs = get_now_prefs(prefs)
    past_overlay = get_past_overlay_prefs(prefs)
    calendar_colors = get_calendar_colors(prefs)
    logo_path = prefs.get("LOGO_PATH", "")  # client will interpret relative path (e.g. /static/logo.png) or URL

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
        "date": today.strftime("%Y-%m-%d"),
        "calendars": []
    }

    calendars = read_calendars_file()
    for label, url, icon in calendars:
        try:
            events = read_ics_events_for_date(url, today)
        except Exception as e:
            err_text = f"ERROR: {type(e).__name__} {str(e)}"
            events = [{"title": err_text, "start": "", "end": ""}]
            print(f"[app.py] Error fetching {url}: {e}")
            traceback.print_exc()
        # For the client, expose label/url/icon and events.
        snapshot["calendars"].append({
            "label": label or url,
            "url": url,
            "icon": icon or "",
            "events": events
        })

    return snapshot

# === Cache refresher ===

def refresh_all_calendars():
    global cached_snapshot
    try:
        print("[app.py] Refreshing calendars...")
        new_snapshot = build_snapshot_for_today()
        with cache_lock:
            cached_snapshot = new_snapshot
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

# === Flask routes ===

@app.route("/")
@app.route("/calendars.html")
def index():
    return render_template("calendars.html")

@app.route("/api/events")
def api_events():
    global cached_snapshot
    with cache_lock:
        snap = cached_snapshot
    if snap is None:
        try:
            print("[app.py] Cache empty on request — building snapshot on-demand.")
            snap = build_snapshot_for_today()
            with cache_lock:
                cached_snapshot = snap
        except Exception as ex:
            print("[app.py] Failed to build snapshot on-demand:", ex)
            traceback.print_exc()
            return jsonify({
                "error": "Failed to build calendar snapshot",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "calendars": []
            }), 500
    return jsonify(snap)

if __name__ == "__main__":
    start_background_refresher()
    app.run(debug=True, host="0.0.0.0", port=5000)