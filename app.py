#!/usr/bin/env python3
#!/usr/bin/env python3
"""
app.py

Flask app that reads calendars from calendars.txt and preferences from preferences.txt,
fetches .ics feeds for today's date, and serves a JSON snapshot at /api/events.

A background thread refreshes the cache every REFRESH_INTERVAL_SECONDS seconds.
"""

from flask import Flask, jsonify, render_template
from datetime import datetime, date
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

# === App & cache globals ===
app = Flask(__name__, template_folder="templates")

cache_lock = threading.Lock()
cached_snapshot = None  # will hold the JSON-like object to return from /api/events

# === Utility functions for reading files ===

def parse_input_line(line):
    """
    Parse a line of calendars.txt. Accepts "Label|URL" or "URL" (no label).
    Returns (label_or_none, url).
    """
    parts = re.split(r'\||\t', line, maxsplit=1)
    if len(parts) == 2:
        label = parts[0].strip()
        url = parts[1].strip()
        if label:
            return label, url
    return None, line.strip()

def read_calendars_file():
    """
    Returns list of (label_or_none, url) entries.
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
            out.append(parse_input_line(line))
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
        summary = str(comp.get("summary") or "")
        dtstart = comp.get("dtstart")
        dtend = comp.get("dtend")
        if not dtstart:
            continue
        start = dtstart.dt
        end = dtend.dt if dtend else start
        # skip all-day events (date objects without time)
        from datetime import date as _date, datetime as _datetime
        if isinstance(start, _date) and not isinstance(start, _datetime):
            continue
        # include only events that start on target_date
        try:
            if start.date() == target_date:
                events.append({"title": summary, "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")})
        except Exception:
            # defensive: if start has no .date(), skip
            continue
    # sort by start
    events.sort(key=lambda e: e.get("start") or "99:99")
    return events

# === Build snapshot ===

def build_snapshot_for_today():
    """
    Build the JSON-like snapshot for today's date based on calendars.txt and preferences.
    This should be safe to call concurrently (but caller should hold cache_lock if writing cached_snapshot).
    """
    today = datetime.now().date()
    prefs = read_preferences_file()
    work_start, work_end = get_work_hours(prefs)
    fit_to_window = get_pref_bool(prefs, "FIT_TO_WINDOW", DEFAULT_FIT_TO_WINDOW)
    header = get_header_prefs(prefs)
    event_prefs = get_event_prefs(prefs)
    now_prefs = get_now_prefs(prefs)
    calendar_colors = get_calendar_colors(prefs)

    snapshot = {
        "work_start_hour": work_start,
        "work_end_hour": work_end,
        "fit_to_window": fit_to_window,
        "header": header,
        "event_prefs": event_prefs,
        "now_prefs": now_prefs,
        "calendar_colors": calendar_colors,
        "date": today.strftime("%Y-%m-%d"),
        "calendars": []
    }

    calendars = read_calendars_file()
    for label, url in calendars:
        try:
            events = read_ics_events_for_date(url, today)
        except Exception as e:
            # Capture the error as an event so UI can show something useful instead of failing
            err_text = f"ERROR: {type(e).__name__} {str(e)}"
            events = [{"title": err_text, "start": "", "end": ""}]
            # You may want to log the full traceback for server-side debugging
            print(f"[app.py] Error fetching {url}: {e}")
            traceback.print_exc()
        snapshot["calendars"].append({"label": label or url, "events": events})

    return snapshot

# === Cache refresher ===

def refresh_all_calendars():
    """
    Rebuilds cached_snapshot by fetching all calendars for today.
    """
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
    """
    Background loop that refreshes the cache periodically.
    Runs forever (daemon thread).
    """
    # Do an immediate refresh on start
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

# Start background refresher at import time
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
    """
    Return the cached snapshot if available. If not, build it on-demand.
    """
    global cached_snapshot
    with cache_lock:
        snap = cached_snapshot
    if snap is None:
        # No cache yet (startup race) — build it now (non-blocking for other requests)
        try:
            print("[app.py] Cache empty on request — building snapshot on-demand.")
            snap = build_snapshot_for_today()
            with cache_lock:
                cached_snapshot = snap
        except Exception as ex:
            print("[app.py] Failed to build snapshot on-demand:", ex)
            traceback.print_exc()
            # Return a minimal informative JSON
            return jsonify({
                "error": "Failed to build calendar snapshot",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "calendars": []
            }), 500
    return jsonify(snap)

# === CLI entrypoint ===

if __name__ == "__main__":
    # start background refresher then run Flask dev server
    start_background_refresher()
    app.run(debug=True, host="0.0.0.0", port=5000)
