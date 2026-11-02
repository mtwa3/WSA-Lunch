#!/usr/bin/env python3
"""
WSA Lower School Lunch  ->  Google Calendar, kept in sync automatically.

Three problems this solves (the old one-shot generator had none of them):

1. STALENESS   Only a rolling window is ever written (default: today .. today+6,
               i.e. one week). Menus that far out are the only ones FLIK can
               still change, so nothing older than a week gets committed.
2. DUPLICATES  Every day keeps one stable identity (UID / event id) forever, and
               each date's content is hashed in a state file. If the menu changes
               the existing calendar entry is *updated in place* (SEQUENCE +1);
               if it did not change, nothing is written at all. Re-running this
               script 1000x never creates a second "Lower School Lunch - Monday".
3. SCHEDULING  Idempotent by design, so it is safe to run from cron / launchd /
               GitHub Actions on a timer. No human in the loop.

Two sync targets (pick one):

  A) FEED  (default, stdlib only, no Google credentials)
     Writes wsa-lower-school-lunch.ics with stable UIDs. Host it at a fixed URL
     (GitHub gist / Pages / any web server) and in Google Calendar add
     "Other calendars -> + -> From URL". Google re-fetches the feed (~8-24 h) and
     replaces its contents: updates appear, duplicates are impossible.

  B) GOOGLE CALENDAR API  (--gcal, needs `pip install google-api-python-client
     google-auth-oauthlib` plus an OAuth client JSON)
     Real upserts into a calendar you choose, in place, reminders off; with
     --prune it also removes entries that fell outside the window.

Usage
    python3 wsa_lunch_sync.py --dry-run                     # what would change
    python3 wsa_lunch_sync.py                               # write rolling feed
    python3 wsa_lunch_sync.py --days 10 --back-days 2       # wider window + history
    python3 wsa_lunch_sync.py --months 2                    # whole current month + next month
    python3 wsa_lunch_sync.py --gist create                 # also publish to a gist
    python3 wsa_lunch_sync.py --gcal --calendar-id primary --prune
    python3 wsa_lunch_sync.py --fixture fixtures/ --start 2026-11-02 --days 7
    python3 wsa_lunch_sync.py --self-test                   # offline sanity checks

Lunch only, Mon-Fri, all-day (no time, no reminder) - matching the earlier file.
"""

import argparse
import datetime as _dt
import glob
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.request

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
API_HOST = "wsa.api.flikisdining.com"
SCHOOL = "lower-school"
MEAL = "lunch"

HORIZON_DAYS = 7        # how far AHEAD to publish (counting today)
BACK_DAYS = 0           # days in the past to keep emitting (0 = nothing older than today)
WEEKDAY_ONLY = True     # Mon-Fri only
ALL_DAY = True          # all-day event -> no time, no pop-up reminder
LUNCH_START_HOUR, LUNCH_START_MINUTE = 11, 30
EVENT_DURATION_MIN = 60

TITLE = "Lower School Lunch"
LOCATION = ""
# Timezone for the "Last checked" line shown in every calendar entry.
# Any IANA name works, e.g. "America/Chicago"; falls back to UTC if unavailable.
DISPLAY_TZ = "UTC"
FEED_NAME = "WSA Lower School Lunch"
OUT_DEFAULT = "wsa-lower-school-lunch.ics"
STATE_DEFAULT = "~/.wsa_lunch_state.json"
REQUEST_DELAY = 0.3
UA = "Mozilla/5.0 (wsa-lunch-calendar-sync)"
CA_BUNDLE = None          # set by --ca-bundle; else certifi is auto-detected

# Google Calendar API only: marks the events we own so --prune can never touch
# your real events. Change it if you run this for several schools.
GCAL_MARK_KEY = "wsaLunchSync"
GCAL_MARK_VAL = SCHOOL + "-" + MEAL
GCAL_TOKEN_CACHE = "~/.wsa_lunch_gcal_token.json"
GCAL_SCOPES = ["https://www.googleapis.com/auth/calendar"]


# ---------------------------------------------------------------------------
# ICS primitives
# ---------------------------------------------------------------------------
def escape_text(value: str) -> str:
    v = str(value)
    v = v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    v = v.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return v


def fold(line: str) -> str:
    """Fold to <=75 octets per RFC 5545 without splitting a UTF-8 character."""
    parts, cur, cur_bytes, limit = [], "", 0, 75
    for ch in line:
        blen = len(ch.encode("utf-8"))
        if cur_bytes + blen > limit:
            parts.append(cur)
            cur, cur_bytes, limit = "", 0, 74
        cur += ch
        cur_bytes += blen
    if cur:
        parts.append(cur)
    return parts[0] + "".join("\r\n " + p for p in parts[1:])


def prop(name: str, value: str) -> str:
    return fold(f"{name}:{value}")


def uid_for(day: _dt.date) -> str:
    """Stable forever for a given date - this is what prevents duplicates."""
    return f"wsa-{SCHOOL}-{MEAL}-{day.isoformat()}@flikisdining.com"


def gcal_id_for(day: _dt.date) -> str:
    """Deterministic Google event id (5-1024 chars of [A-Za-z0-9._@~-])."""
    return f"wsalunch{day.strftime('%Y%m%d')}{SCHOOL[:3]}"


def format_check_time(now=None) -> str:
    """Human-readable 'when did we last pull this' stamp for the feed."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        now = now.astimezone(ZoneInfo(DISPLAY_TZ))
    except Exception:                                # noqa: BLE001 - no tzdata -> UTC
        now = now.astimezone(_dt.timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M %Z")


# ---------------------------------------------------------------------------
# Menu fetching / parsing
# ---------------------------------------------------------------------------
def monday_on_or_before(d: _dt.date) -> _dt.date:
    return d - _dt.timedelta(days=d.weekday())


def ssl_context():
    """Trust store that works on macOS python.org builds.

    Those builds do not read the Keychain or /etc/ssl, so plain
    ssl.create_default_context() raises CERTIFICATE_VERIFY_FAILED for every
    HTTPS host. certifi ships a CA bundle; prefer it when available.
    """
    if CA_BUNDLE:
        return ssl.create_default_context(cafile=os.path.expanduser(CA_BUNDLE))
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:                                # noqa: BLE001 - fall through
        return ssl.create_default_context()


def friendly_fetch_error(err) -> str:
    text = str(err)
    if "CERTIFICATE_VERIFY_FAILED" in text:
        ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        return (text +
                "\n    ! This Python build has no CA bundle (typical for python.org installs on"
                " macOS). Fix once, easiest first:\n"
                f"        \"/Applications/Python {ver}/Install Certificates.command\"\n"
                "        python3 -m pip install --upgrade certifi   # auto-detected by this script\n"
                "        run me with /usr/bin/python3 instead (uses Apple's trust store)\n"
                "        one-off workaround:  --ca-bundle /etc/ssl/cert.pem")
    return text


def fetch_week(anchor: _dt.date, fixture_dir=None, retries=3):
    """One Nutrislice week (7 days). With fixture_dir, read local JSON instead."""
    if fixture_dir:
        for path in (os.path.join(fixture_dir, anchor.isoformat() + ".json"),
                     os.path.join(fixture_dir, anchor.strftime("%Y%m%d") + ".json")):
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        hits = sorted(glob.glob(os.path.join(fixture_dir, "*.json")))
        if not hits:
            raise RuntimeError(f"no fixture JSON in {fixture_dir}")
        with open(hits[0], encoding="utf-8") as f:
            return json.load(f)

    url = (f"https://{API_HOST}/menu/api/weeks/school/{SCHOOL}"
           f"/menu-type/{MEAL}/{anchor.year}/{anchor.month}/{anchor.day}/?format=json")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = ssl_context()
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:                      # noqa: BLE001 - retry anything
            last = e
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                break                               # pointless to retry a trust problem
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {friendly_fetch_error(last)}")


def parse_day(day: dict):
    """API day -> list of (kind, text)."""
    lines = []
    for it in day.get("menu_items", []) or []:
        if it.get("is_holiday"):
            lines.append(("holiday", it.get("text") or "Holiday - no menu"))
            continue
        if it.get("is_station_header") or it.get("is_section_title"):
            txt = (it.get("text") or "").strip()
            if txt:
                lines.append(("header", txt))
            continue
        food = it.get("food")
        if food:
            name = (food.get("name") or "").strip()
            desc = (food.get("description") or "").strip()
            extra = []
            if desc and desc.lower() != name.lower():
                extra.append(desc)
            amt, unit = food.get("serving_size_amount"), food.get("serving_size_unit")
            if amt is not None:
                s = f"{amt:g} {unit}".strip()
                if s:
                    extra.append(s)
            s = name + (f" ({'; '.join(extra)})" if extra else "")
            if s:
                lines.append(("food", s))
            continue
        txt = (it.get("text") or "").strip()
        if txt:
            lines.append(("text", txt))
    return lines


def render_description(lines) -> str:
    out = []
    for kind, val in lines:
        if kind == "header":
            out.append(val.upper())
        elif kind == "food":
            out.append("\u2022 " + val)
        else:
            out.append(val)
    return "\n".join(out)


def collect_days(start: _dt.date, end: _dt.date, fixture_dir=None, stats=None):
    """Fetch the weeks that tile [start, end] and return the in-range days."""
    cur, seen = monday_on_or_before(start), {}
    while cur <= end:
        try:
            data = fetch_week(cur, fixture_dir)
            if stats is not None:
                stats["ok"] += 1
        except Exception as e:                      # noqa: BLE001
            print(f"  ! {cur}: {e}", file=sys.stderr)
            if stats is not None:
                stats["failed"] += 1
            data = None
        for day in (data or {}).get("days", []):
            iso = day.get("date")
            if not iso:
                continue
            d = _dt.date.fromisoformat(iso)
            if start <= d <= end:
                seen[iso] = day
        if fixture_dir is None and REQUEST_DELAY:
            time.sleep(REQUEST_DELAY)
        cur += _dt.timedelta(days=7)
    return [seen[k] for k in sorted(seen)]


def has_content(day: dict) -> bool:
    return any(k in ("food", "text", "holiday") for k, _ in parse_day(day))


# ---------------------------------------------------------------------------
# Day model + change detection
# ---------------------------------------------------------------------------
class DayEvent:
    def __init__(self, date: _dt.date, description: str, published: bool,
                 checked_at: str = None):
        self.date = date
        self.base_description = description
        self.checked_at = checked_at
        self.published = published
        self.summary = f"{TITLE} - {date.strftime('%A')}"
        # The "Last checked" line is deliberately NOT part of the content hash:
        # the clock moves on every run, but a moved clock is not a menu change,
        # so SEQUENCE/DTSTAMP stay stable while the visible timestamp refreshes.
        if checked_at:
            self.description = description + "\nLast checked " + checked_at
        else:
            self.description = description
        # Sort lines so a re-ordering upstream is not treated as a menu change.
        payload = json.dumps([self.summary, sorted(self.base_description.splitlines())],
                             ensure_ascii=False)
        self.content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        self.sequence = 1
        self.status = "new"           # new | updated | unchanged
        self.stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def to_events(days, checked_at: str = None):
    events = []
    for day in days:
        d = _dt.date.fromisoformat(day["date"])
        body = render_description(parse_day(day))
        if not body:
            # FLIK fills menus in gradually (~a week ahead): an empty day almost
            # always means "not entered yet", not "there is no lunch". The
            # "Last checked" line added by DayEvent tells how recent that is.
            body = ("Menu not yet published"
                    if not day.get("has_unpublished_menus")
                    else "(Some stations unpublished)")
        elif day.get("has_unpublished_menus"):
            body = "(Some stations unpublished)\n" + body
        events.append(DayEvent(d, body, has_content(day), checked_at))
    return events


def load_state(path):
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                # noqa: BLE001 - first run
        return {"days": {}}


def apply_state(events, state):
    """Give each event a SEQUENCE, a DTSTAMP and a status.

    DTSTAMP only moves when the menu actually changes, so a re-run with no edits
    produces a byte-identical feed - which is what lets the CI commit step stay
    quiet instead of churning a new commit every single day.
    """
    store = state.setdefault("days", {})
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for ev in events:
        rec = store.get(ev.date.isoformat())
        if not rec:
            ev.sequence, ev.status, ev.stamp = 1, "new", now
        elif rec.get("hash") != ev.content_hash:
            ev.sequence = int(rec.get("sequence", 1)) + 1
            ev.status, ev.stamp = "updated", now
        else:
            ev.sequence = int(rec.get("sequence", 1))
            ev.status = "unchanged"
            ev.stamp = rec.get("stamp") or now
    return events


def commit_state(events, state, path):
    store = state.setdefault("days", {})
    for ev in events:
        if ev.status == "unchanged":
            continue
        rec = store.setdefault(ev.date.isoformat(), {})
        rec.update({"hash": ev.content_hash, "sequence": ev.sequence, "stamp": ev.stamp})
    state["last_run"] = _dt.datetime.now().isoformat(timespec="seconds")
    with open(os.path.expanduser(path), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Output A: subscription feed
# ---------------------------------------------------------------------------
def build_feed(events):
    fallback = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    head = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//wsa//lunch-menu-sync//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        prop("X-WR-CALNAME", escape_text(FEED_NAME)),
        "X-PUBLISHED-TTL:PT12H",
    ]
    body = []
    for ev in sorted(events, key=lambda e: e.date):
        blocks = [
            "BEGIN:VEVENT",
            prop("UID", uid_for(ev.date)),
            prop("DTSTAMP", getattr(ev, "stamp", None) or fallback),
            prop("SEQUENCE", str(ev.sequence)),
        ]
        if ALL_DAY:
            blocks.append(prop("DTSTART;VALUE=DATE", ev.date.strftime("%Y%m%d")))
            blocks.append(prop("DTEND;VALUE=DATE",
                               (ev.date + _dt.timedelta(days=1)).strftime("%Y%m%d")))
        else:
            s = _dt.datetime.combine(ev.date, _dt.time(LUNCH_START_HOUR, LUNCH_START_MINUTE))
            blocks.append(prop("DTSTART", s.strftime("%Y%m%dT%H%M%S")))
            blocks.append(prop("DTEND",
                               (s + _dt.timedelta(minutes=EVENT_DURATION_MIN)).strftime("%Y%m%dT%H%M%S")))
        blocks += [prop("SUMMARY", escape_text(ev.summary)),
                   prop("DESCRIPTION", escape_text(ev.description))]
        if LOCATION:
            blocks.append(prop("LOCATION", escape_text(LOCATION)))
        blocks += ["CATEGORIES:SCHOOL", "STATUS:CONFIRMED", "END:VEVENT"]
        body.append("\r\n".join(blocks))
    return "\r\n".join(head + body + ["END:VCALENDAR"]) + "\n"


# ---------------------------------------------------------------------------
# Output B: Google Calendar API upsert
# ---------------------------------------------------------------------------
def gcal_service(client_secrets):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:                          # pragma: no cover
        raise SystemExit("Need the Google client libs:\n"
                         "  pip install google-api-python-client google-auth-oauthlib\n"
                         f"({e})")
    cache = os.path.expanduser(GCAL_TOKEN_CACHE)
    creds = None
    if os.path.exists(cache):
        creds = Credentials.from_authorized_user_file(cache, GCAL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets, GCAL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(cache, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def gcal_event_body(ev):
    body = {
        "id": gcal_id_for(ev.date),
        "iCalUID": uid_for(ev.date),
        "summary": ev.summary,
        "description": ev.description,
        "extendedProperties": {"private": {GCAL_MARK_KEY: GCAL_MARK_VAL}},
        "reminders": {"useDefault": False},           # no reminder, as requested
    }
    if ALL_DAY:
        body["start"] = {"date": ev.date.isoformat()}
        body["end"] = {"date": (ev.date + _dt.timedelta(days=1)).isoformat()}
    else:
        tz = time.tzname[0] or "America/New_York"
        s = _dt.datetime.combine(ev.date, _dt.time(LUNCH_START_HOUR, LUNCH_START_MINUTE))
        e = s + _dt.timedelta(minutes=EVENT_DURATION_MIN)
        body["start"] = {"dateTime": s.isoformat(), "timeZone": tz}
        body["end"] = {"dateTime": e.isoformat(), "timeZone": tz}
    if LOCATION:
        body["location"] = LOCATION
    return body


def gcal_sync(svc, calendar_id, events, prune=False, horizon_end=None, keep_history=True):
    """Create-or-update in place. Unchanged days make zero API calls."""
    from googleapiclient.errors import HttpError
    created = updated = skipped = deleted = 0
    marker = f"{GCAL_MARK_KEY}={GCAL_MARK_VAL}"

    for ev in sorted(events, key=lambda e: e.date):
        if ev.status == "unchanged":
            skipped += 1
            continue
        body = gcal_event_body(ev)
        try:
            svc.events().patch(calendarId=calendar_id, eventId=body["id"], body=body).execute()
        except HttpError as err:
            if err.resp.status != 404:
                raise
            found = svc.events().list(calendarId=calendar_id, iCalUID=body["iCalUID"],
                                      showDeleted=False).execute().get("items", [])
            if found:                                  # older import under a random id
                body.pop("id", None)
                svc.events().patch(calendarId=calendar_id, eventId=found[0]["id"],
                                   body=body).execute()
            else:
                svc.events().import_(calendarId=calendar_id, body=body).execute()
        if ev.status == "new":
            created += 1
        else:
            updated += 1

    if prune and horizon_end:
        stale = svc.events().list(calendarId=calendar_id, privateExtendedProperty=[marker],
                                  timeMin=horizon_end.isoformat(), singleEvents=True,
                                  orderBy="startTime", maxResults=250).execute()
        for item in stale.get("items", []):
            svc.events().delete(calendarId=calendar_id, eventId=item["id"]).execute()
            deleted += 1
        if not keep_history:
            past = svc.events().list(calendarId=calendar_id, privateExtendedProperty=[marker],
                                     timeMax=_dt.date.today().isoformat(), singleEvents=True,
                                     maxResults=250).execute()
            for item in past.get("items", []):
                svc.events().delete(calendarId=calendar_id, eventId=item["id"]).execute()
                deleted += 1
    return created, updated, skipped, deleted


# ---------------------------------------------------------------------------
# GitHub gist push (keeps the feed URL constant -> Google just re-reads it)
# ---------------------------------------------------------------------------
def gist_request(method, path, token, payload=None):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": f"token {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def push_gist(gist_id, filename, content, token, public=False):
    if not gist_id:
        resp = gist_request("POST", "/gists", token, {
            "public": bool(public),
            "description": FEED_NAME + " (rolling window)",
            "files": {filename: {"content": content}}})
        return resp["id"], resp.get("html_url", "")
    resp = gist_request("PATCH", f"/gists/{gist_id}", token,
                        {"files": {filename: {"content": content}}})
    return resp["id"], resp.get("html_url", "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def window(today=None, horizon=HORIZON_DAYS, back=BACK_DAYS):
    today = today or _dt.date.today()
    return (today - _dt.timedelta(days=back),
            today + _dt.timedelta(days=max(horizon, 1) - 1))


def month_window(today=None, months=2):
    """Whole calendar months, anchored on the current one.

    Run on Nov 1 with months=2 -> Nov 1 .. Dec 31. Unlike the rolling day
    window, this keeps the *entire* current month visible until the month
    turns, and always shows everything FLIK has published for next month.
    """
    today = today or _dt.date.today()
    months = max(months, 1)
    y, m = today.year, today.month + months - 1
    while m > 12:
        m -= 12
        y += 1
    nxt = _dt.date(y + 1, 1, 1) if m == 12 else _dt.date(y, m + 1, 1)
    return today.replace(day=1), nxt - _dt.timedelta(days=1)


def build_events(args, state, checked_at: str = None):
    today = _dt.date.today()
    win = month_window(today, args.months) if args.months else window(today, args.days, args.back_days)
    start = _dt.date.fromisoformat(args.start) if args.start else win[0]
    end = _dt.date.fromisoformat(args.end) if args.end else win[1]
    print(f"Window {start} .. {end}  ({'Mon-Fri only' if WEEKDAY_ONLY else 'all days'}, "
          f"{'all-day / no reminder' if ALL_DAY else 'timed'}, lunch only)")
    stats = {"ok": 0, "failed": 0}
    days = collect_days(start, end, args.fixture, stats)
    if stats["failed"] and not days:
        print(f"\n!! All {stats['failed']} week fetches failed - nothing written, state untouched."
              "\n!! Fix the cause above and re-run (exit status 1 so cron/Actions reports it).",
              file=sys.stderr)
        return start, end, [], stats
    if stats["failed"]:
        print(f"! {stats['failed']} of {stats['ok'] + stats['failed']} weeks failed; "
              "publishing what did arrive.", file=sys.stderr)
    if WEEKDAY_ONLY:
        days = [d for d in days if _dt.date.fromisoformat(d["date"]).weekday() < 5]
    if not args.include_placeholders:
        days = [d for d in days if has_content(d)]
    return start, end, apply_state(to_events(days, checked_at), state), stats


def main(argv=None):
    ap = argparse.ArgumentParser(description="Keep a rolling WSA lunch calendar in sync.")
    ap.add_argument("--days", type=int, default=HORIZON_DAYS,
                    help="days to publish AHEAD, counting today (default %(default)s)")
    ap.add_argument("--months", type=int, default=None, metavar="N",
                    help="whole calendar months starting with the current one (2 = this + next); "
                         "overrides --days/--back-days")
    ap.add_argument("--back-days", type=int, default=BACK_DAYS, dest="back_days",
                    help="also keep N past days in the output (default %(default)s)")
    ap.add_argument("--start", default=None, help="override window start YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="override window end YYYY-MM-DD")
    ap.add_argument("--out", default=OUT_DEFAULT, help="feed path (default %(default)s)")
    ap.add_argument("--state", default=STATE_DEFAULT, help="state file path")
    ap.add_argument("--include-placeholders", action="store_true",
                    help="also emit days whose menu is not published yet")
    ap.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    ap.add_argument("--print-feed", action="store_true", dest="print_feed",
                    help="write the .ics to stdout instead of a file")
    ap.add_argument("--gist", default=None,
                    help="push the feed to this gist id, or 'create' for a new one; needs GIST_TOKEN")
    ap.add_argument("--gist-file", default=OUT_DEFAULT, dest="gist_file")
    ap.add_argument("--gcal", action="store_true", help="sync straight into Google Calendar")
    ap.add_argument("--calendar-id", default="primary", dest="calendar_id")
    ap.add_argument("--client-secrets", default="client_secret.json", dest="client_secrets")
    ap.add_argument("--prune", action="store_true",
                    help="with --gcal: delete our events that fall past the window")
    ap.add_argument("--drop-history", action="store_true",
                    help="with --prune: also delete our events before today")
    ap.add_argument("--ca-bundle", default=None, dest="ca_bundle",
                    help="path to a CA bundle / pem file (or 'certifi') if Python has no trust store")
    ap.add_argument("--fixture", default=None,
                    help="read API JSON from a directory instead of the network (testing)")
    ap.add_argument("--self-test", action="store_true", dest="self_test")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    global CA_BUNDLE
    if args.ca_bundle == "certifi":
        import certifi
        CA_BUNDLE = certifi.where()
    elif args.ca_bundle:
        CA_BUNDLE = args.ca_bundle

    state = load_state(args.state)
    checked_at = format_check_time()
    start, end, events, stats = build_events(args, state, checked_at)
    if stats["failed"] and not events:
        return 1                       # total outage: fail loudly, write nothing

    counts = {k: sum(1 for e in events if e.status == k) for k in ("new", "updated", "unchanged")}
    print(f"{len(events)} school days -> new {counts['new']}, "
          f"menu-changed {counts['updated']}, unchanged {counts['unchanged']}")
    glyphs = {"new": "+", "updated": "~", "unchanged": "="}
    for ev in events:
        print(f"  {glyphs[ev.status]} {ev.date} {ev.date.strftime('%a')}  seq={ev.sequence}  "
              f"{ev.description.splitlines()[0][:62]}")

    if args.dry_run:
        print("\n[dry-run] nothing written.")
        return 0

    if args.gcal:
        svc = gcal_service(args.client_secrets)
        c, u, s, d = gcal_sync(svc, args.calendar_id, events, prune=args.prune,
                               horizon_end=end + _dt.timedelta(days=1),
                               keep_history=not args.drop_history)
        commit_state(events, state, args.state)
        print(f"\nGoogle Calendar '{args.calendar_id}': created {c}, updated {u}, "
              f"skipped {s} unchanged, pruned {d} outside the window.")
        return 0

    feed = build_feed(events)
    if args.print_feed:
        sys.stdout.write(feed)
    else:
        with open(args.out, "w", encoding="utf-8", newline="") as f:
            f.write(feed)
        commit_state(events, state, args.state)
        print(f"\nWrote {len(events)} events -> {args.out}")
        print("State      -> " + os.path.expanduser(args.state))
        print("Next run only touches days whose menu actually changed.")

    if args.gist:
        token = os.environ.get("GIST_TOKEN")
        if not token:
            print("! GIST_TOKEN is not set; skipped the gist push.", file=sys.stderr)
            return 1
        gid, url = push_gist(None if args.gist == "create" else args.gist,
                             args.gist_file, feed, token)
        print(f"Gist updated: {gid}  ({url})")
        print("Google Calendar -> Other calendars -> + -> From URL -> paste the gist's RAW url.")
    return 0


# ---------------------------------------------------------------------------
# Offline self-test (no network): windowing, UID stability, dedupe, folding
# ---------------------------------------------------------------------------
def _fake_week(anchor: _dt.date, mutate=()):
    days = []
    for off in range(7):
        d = anchor + _dt.timedelta(days=off)
        items = [{"is_station_header": True, "text": "Chef's Table"},
                 {"food": {"name": f"Day {d.day} Entree", "description": "",
                           "serving_size_amount": 1, "serving_size_unit": "each"}}]
        if d.isoformat() in mutate:
            items.append({"food": {"name": "Added Item", "description": "",
                                   "serving_size_amount": None, "serving_size_unit": None}})
        days.append({"date": d.isoformat(), "menu_items": items})
    return {"days": days}


def self_test():
    import tempfile
    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        ok = ok and bool(cond)

    today = _dt.date(2026, 11, 2)                      # a Monday
    start, end = window(today)
    check("default window is exactly one week", (end - start).days == 6)
    check("--months 2 covers the current + next month",
          month_window(_dt.date(2026, 11, 17), 2) == (_dt.date(2026, 11, 1), _dt.date(2026, 12, 31)))
    check("month window rolls over the year",
          month_window(_dt.date(2026, 12, 3), 2) == (_dt.date(2026, 12, 1), _dt.date(2027, 1, 31)))
    mondays = {monday_on_or_before(start + _dt.timedelta(days=i)) for i in range(7)}
    check("a 7-day window costs at most 2 API calls", len(mondays) <= 2)
    mid = {monday_on_or_before(_dt.date(2026, 11, 4) + _dt.timedelta(days=i)) for i in range(7)}
    check("...and a mid-week start spans exactly 2 weeks", len(mid) == 2)

    with tempfile.TemporaryDirectory() as tmp:
        fdir, state = os.path.join(tmp, "fx"), os.path.join(tmp, "state.json")
        os.makedirs(fdir)

        def run(mutate=()):
            for anchor in mondays:
                with open(os.path.join(fdir, anchor.isoformat() + ".json"), "w",
                          encoding="utf-8") as f:
                    json.dump(_fake_week(anchor, mutate), f)
            days = collect_days(start, end, fdir)
            days = [d for d in days if _dt.date.fromisoformat(d["date"]).weekday() < 5]
            # Fixed clock so the "Last checked" line cannot break the
            # byte-identical / no-change assertions below.
            evs = apply_state(to_events(days, "2026-11-02 07:00 UTC"), load_state(state))
            return evs

        evs = run()
        check("7-day window yields 5 weekdays", len(evs) == 5)
        check("nothing further out than a week", max(e.date for e in evs) <= end)
        check("all new on first pass", all(e.status == "new" for e in evs))

        feed = build_feed(evs)
        check("lines folded to <=75 octets",
              all(len(l.encode()) <= 75 for l in feed.split("\r\n")))
        check("valid calendar envelope",
              feed.startswith("BEGIN:VCALENDAR") and feed.rstrip().endswith("END:VCALENDAR"))
        uids = re.findall(r"UID:([^\r]+)", feed)
        check("5 events, one stable UID per date",
              feed.count("BEGIN:VEVENT") == 5 and len(set(uids)) == 5
              and uids[0].startswith("wsa-lower-school-lunch-"))
        commit_state(evs, load_state(state), state)

        evs2 = run()                                    # identical menus
        check("re-run with no menu change -> all unchanged", all(e.status == "unchanged" for e in evs2))
        check("SEQUENCE does not drift when nothing changed", [e.sequence for e in evs2] == [1] * 5)
        check("re-run is byte-identical (so CI commits stay quiet)",
              build_feed(evs2) == build_feed(run()))

        # A moved "last checked" clock must NOT count as a menu change...
        evs_clock = apply_state(to_events(
            [d for d in collect_days(start, end, fdir)
             if _dt.date.fromisoformat(d["date"]).weekday() < 5],
            "2099-01-01 00:00 UTC"), load_state(state))
        check("every entry carries a 'Last checked' line",
              all(e.description.endswith("\nLast checked 2099-01-01 00:00 UTC") for e in evs_clock))
        check("...and leaves SEQUENCE/status untouched",
              all(e.status == "unchanged" for e in evs_clock) and [e.sequence for e in evs_clock] == [1] * 5)

        evs3 = run(mutate=("2026-11-04",))              # FLIK edits Wednesday
        wed = next(e for e in evs3 if e.date == _dt.date(2026, 11, 4))
        others = [e for e in evs3 if e.date != _dt.date(2026, 11, 4)]
        check("edited day flagged 'updated'", wed.status == "updated")
        check("edited day bumps SEQUENCE to 2", wed.sequence == 2)
        check("same UID for the edited day (update in place, not a copy)",
              uid_for(wed.date) == "wsa-lower-school-lunch-2026-11-04@flikisdining.com")
        check("unedited days stay 'unchanged' -> zero writes", all(e.status == "unchanged" for e in others))
        check("event count stays 5 after an edit",
              build_feed(evs3).count("BEGIN:VEVENT") == 5)
        check("google event id is deterministic + API-legal",
              re.fullmatch(r"[A-Za-z0-9._@~-]{5,1024}", gcal_id_for(wed.date)) is not None)

    print("\nSELF-TEST: " + ("all good" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
