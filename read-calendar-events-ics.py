#!/usr/bin/env python3
"""
Read events from an .ics feed for a particular date.

Usage:
    ./read-calendar-events-ics.py <ics_url> YYYY-MM-DD [--tz Europe/London]

This version is recurrence-aware: it handles recurring masters and exception
instances (VEVENTs with RECURRENCE-ID) instead of only checking each VEVENT as
an isolated one-off event.
"""

import sys
from datetime import date, datetime, time, timedelta

import requests
from icalendar import Calendar
from dateutil import parser as dtparser
from dateutil import tz
from dateutil.rrule import rrulestr

DEFAULT_TZ = "Europe/London"


def parse_args():
    import argparse

    p = argparse.ArgumentParser(description="Read events from an .ics feed for a particular date.")
    p.add_argument("ics_url", help="URL of the .ics feed")
    p.add_argument("date", help="Target date YYYY-MM-DD")
    p.add_argument("--tz", default=DEFAULT_TZ, help=f"Timezone (default: {DEFAULT_TZ})")
    return p.parse_args()


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
        return datetime.combine(raw, time.min).replace(tzinfo=target_tz)

    return None


def is_date_only(value):
    raw = get_raw_value(value)
    return isinstance(raw, date) and not isinstance(raw, datetime)


def normalize_key(value, target_tz):
    """Normalize a recurrence key so DTSTART and RECURRENCE-ID can be compared safely."""
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


def event_overlaps_date(start_value, end_value, target_date, target_tz):
    """Return True when an event span overlaps the target date."""
    start_dt = to_datetime(start_value, target_tz)
    if start_dt is None:
        return False

    if end_value is None:
        end_dt = start_dt
    else:
        end_dt = to_datetime(end_value, target_tz)
        if end_dt is None:
            end_dt = start_dt

    day_start = datetime.combine(target_date, time.min).replace(tzinfo=target_tz)
    day_end = day_start + timedelta(days=1)
    return start_dt < day_end and end_dt > day_start


def format_span(start_dt, end_dt, target_date, target_tz, all_day=False):
    """Format the visible portion of an event on the target date."""
    if all_day:
        return ("", "")

    day_start = datetime.combine(target_date, time.min).replace(tzinfo=target_tz)
    day_end = day_start + timedelta(days=1)
    display_start = max(start_dt, day_start)
    display_end = min(end_dt, day_end)

    if display_end >= day_end:
        end_str = "23:59"
    else:
        end_str = display_end.strftime("%H:%M")

    return (display_start.strftime("%H:%M"), end_str)


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
    if text.startswith("RRULE:"):
        text = text[len("RRULE:") :]
    return text or None


def collect_override_keys(components, target_tz):
    """Collect recurrence IDs that are overridden by explicit VEVENT instances."""
    keys = set()
    for comp in components:
        if comp.name != "VEVENT":
            continue
        recur_id = get_raw_value(comp.get("recurrence-id"))
        if recur_id is None:
            continue
        keys.add(normalize_key(recur_id, target_tz))
    return keys


def expand_recurrence_component(comp, target_date, target_tz, override_keys):
    """Yield matching event spans for a recurring master VEVENT."""
    start_value = comp.get("dtstart")
    end_value = comp.get("dtend")
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

    rrule_text = rrule_to_text(comp.get("rrule"))
    if not rrule_text:
        # Fall back to a plain single event if RRULE is absent.
        if event_overlaps_date(start_value, end_value, target_date, target_tz):
            yield (start_dt, end_dt, all_day, normalize_key(start_value, target_tz))
        return

    try:
        rule = rrulestr(rrule_text, dtstart=start_dt)
    except Exception:
        # If the RRULE cannot be parsed, at least keep the master event if it overlaps.
        if event_overlaps_date(start_value, end_value, target_date, target_tz):
            yield (start_dt, end_dt, all_day, normalize_key(start_value, target_tz))
        return

    day_start = datetime.combine(target_date, time.min).replace(tzinfo=target_tz) - timedelta(days=1)
    day_end = datetime.combine(target_date, time.min).replace(tzinfo=target_tz) + timedelta(days=1)

    # Search a 1-day buffer before the target date so overnight recurrences are not missed.
    for occ_start in rule.between(day_start, day_end, inc=True):
        occ_end = occ_start + duration
        if not event_overlaps_date(occ_start, occ_end, target_date, target_tz):
            continue
        occ_key = normalize_key(occ_start, target_tz) if not all_day else normalize_key(occ_start.date(), target_tz)
        if occ_key in override_keys:
            continue
        yield (occ_start, occ_end, all_day, occ_key)


def read_ics_events_for_date(ics_url, target_date_str, tz_name=DEFAULT_TZ):
    target_date = dtparser.parse(target_date_str).date()
    target_tz = tz.gettz(tz_name)
    if target_tz is None:
        raise ValueError(f"Unknown timezone '{tz_name}'")

    headers = {"User-Agent": "read-calendar-events-ics/2.0"}
    resp = requests.get(ics_url, headers=headers, timeout=20)
    resp.raise_for_status()

    cal = Calendar.from_ical(resp.content)
    components = [comp for comp in cal.walk() if getattr(comp, "name", None) == "VEVENT"]
    override_keys = collect_override_keys(components, target_tz)

    events = []
    seen = set()

    for comp in components:
        summary = str(comp.get("summary") or "").strip()
        start_value = comp.get("dtstart")
        end_value = comp.get("dtend")
        recur_id = comp.get("recurrence-id")
        rrule_text = rrule_to_text(comp.get("rrule"))

        if recur_id is not None:
            # Explicit exception instance.
            if start_value is None:
                start_value = recur_id
            if not event_overlaps_date(start_value, end_value, target_date, target_tz):
                continue

            start_dt = to_datetime(start_value, target_tz)
            end_dt = get_end_datetime(comp, start_value, target_tz) if start_dt is not None else None
            if start_dt is None:
                continue
            if end_dt is None:
                end_dt = start_dt

            key = (summary, normalize_key(recur_id, target_tz))
            if key in seen:
                continue
            seen.add(key)

            events.append((summary, *format_span(start_dt, end_dt, target_date, target_tz, all_day=is_date_only(start_value))))
            continue

        if rrule_text:
            for occ_start, occ_end, all_day, occ_key in expand_recurrence_component(comp, target_date, target_tz, override_keys):
                key = (summary, occ_key)
                if key in seen:
                    continue
                seen.add(key)
                events.append((summary, *format_span(occ_start, occ_end, target_date, target_tz, all_day=all_day)))
            continue

        # Plain one-off event.
        if start_value is None:
            continue
        if not event_overlaps_date(start_value, end_value, target_date, target_tz):
            continue

        start_dt = to_datetime(start_value, target_tz)
        end_dt = get_end_datetime(comp, start_value, target_tz) if start_dt is not None else None
        if start_dt is None:
            continue
        if end_dt is None:
            end_dt = start_dt

        key = (summary, normalize_key(start_value, target_tz))
        if key in seen:
            continue
        seen.add(key)

        events.append((summary, *format_span(start_dt, end_dt, target_date, target_tz, all_day=is_date_only(start_value))))

    events.sort(
        key=lambda ev: (
            1 if not ev[1] else 0,
            datetime.strptime(ev[1], "%H:%M").time() if ev[1] else time.min,
            ev[0].lower(),
        )
    )
    return events


def pretty_print(events):
    if not events:
        print("No events found on that date.")
        return

    for title, start_str, end_str in events:
        if start_str and end_str:
            print(f"{title} - {start_str} - {end_str}")
        else:
            print(f"{title} (All day)")


if __name__ == "__main__":
    args = parse_args()
    try:
        events = read_ics_events_for_date(args.ics_url, args.date, args.tz)
    except Exception as exc:
        print("Error:", exc)
        sys.exit(2)
    pretty_print(events)
