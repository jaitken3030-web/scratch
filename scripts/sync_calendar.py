#!/usr/bin/env python3
"""Regenerates the KID_EVENTS block in daily-command-log.html from live Google Calendar data.

Run by .github/workflows/sync-calendar.yml on a schedule. Requires env vars:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
Optional:
  GOOGLE_CALENDAR_ID (defaults to the Jesse & Erica family calendar)
  SYNC_KEYWORDS (comma-separated, defaults to "karate,swim meet")
"""
import os
import re
import sys
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("America/New_York")
HTML_PATH = os.path.join(os.path.dirname(__file__), "..", "daily-command-log.html")
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "l43h7g27u9q6jirei2k139eov0@group.calendar.google.com")
KEYWORDS = [k.strip().lower() for k in os.environ.get("SYNC_KEYWORDS", "karate,swim meet").split(",") if k.strip()]
LOOKBACK_DAYS = 1
LOOKAHEAD_DAYS = 21


def get_access_token():
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def list_events(token, time_min, time_max):
    events = []
    page_token = None
    url = f"https://www.googleapis.com/calendar/v3/calendars/{quote(CALENDAR_ID, safe='')}/events"
    while True:
        params = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        }
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        events.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return events


def to_minutes(dt):
    return dt.hour * 60 + dt.minute


def clean_label(summary):
    label = re.sub(r"^[~*]+", "", summary).strip()
    label = re.sub(r"\s*-\s*Beginners$", "", label)
    label = re.sub(r"\bClass\b", "class", label)
    return label


def js_string(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_kid_events_block(kid_events):
    lines = ["  const KID_EVENTS = {"]
    for date_key in sorted(kid_events):
        entries = ", ".join(
            "{ start: %d, end: %d, label: %s }" % (e["start"], e["end"], js_string(e["label"]))
            for e in kid_events[date_key]
        )
        lines.append(f"    '{date_key}': [{entries}],")
    lines.append("  };")
    return "\n".join(lines)


def main():
    now = datetime.now(TZ)
    time_min = now - timedelta(days=LOOKBACK_DAYS)
    time_max = now + timedelta(days=LOOKAHEAD_DAYS)

    token = get_access_token()
    events = list_events(token, time_min, time_max)

    kid_events = {}
    for ev in events:
        summary = ev.get("summary", "")
        if not any(k in summary.lower() for k in KEYWORDS):
            continue
        start = ev.get("start", {})
        end = ev.get("end", {})
        if "dateTime" not in start or "dateTime" not in end:
            continue
        start_dt = datetime.fromisoformat(start["dateTime"]).astimezone(TZ)
        end_dt = datetime.fromisoformat(end["dateTime"]).astimezone(TZ)
        date_key = start_dt.strftime("%Y-%m-%d")
        kid_events.setdefault(date_key, []).append(
            {"start": to_minutes(start_dt), "end": to_minutes(end_dt), "label": clean_label(summary)}
        )

    html = open(HTML_PATH, encoding="utf-8").read()

    events_pattern = re.compile(
        r"(// AUTO-SYNC:KID_EVENTS:START.*?\n)  const KID_EVENTS = \{.*?\n  \};(\n  // AUTO-SYNC:KID_EVENTS:END)",
        re.DOTALL,
    )
    if not events_pattern.search(html):
        print("KID_EVENTS markers not found in HTML", file=sys.stderr)
        sys.exit(1)
    new_block = build_kid_events_block(kid_events)
    html = events_pattern.sub(lambda m: m.group(1) + new_block + m.group(2), html, count=1)

    date_pattern = re.compile(r"<!--AUTO-SYNC:DATE-->.*?<!--/AUTO-SYNC:DATE-->")
    today_label = now.strftime("%b %-d, %-I:%M%p")
    html = date_pattern.sub(f"<!--AUTO-SYNC:DATE-->{today_label}<!--/AUTO-SYNC:DATE-->", html)

    open(HTML_PATH, "w", encoding="utf-8").write(html)
    print(f"Synced {sum(len(v) for v in kid_events.values())} event(s) across {len(kid_events)} day(s)")


if __name__ == "__main__":
    main()
