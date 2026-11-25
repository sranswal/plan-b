"""Interchangeable calendar providers for the Context Service."""

import time as clock
import uuid
from datetime import UTC, date, datetime, time, timedelta
from threading import Lock
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from services.action_store import aware, verify_approval, version
from services.config import setting
from services.context.google_oauth import GoogleConnection, GoogleError


ZONE_NAME = "Asia/Kolkata"
WINDOW_FIELDS = {"window_start", "window_end"}


def google_event_id(proposal_id):
    try:
        return "planb" + uuid.UUID(proposal_id).hex
    except ValueError:
        raise HTTPException(422, "Invalid Google event proposal ID") from None


def bounds(start, end):
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise HTTPException(422, "Provide both start and end timestamps")
    first, last = aware(start), aware(end)
    if not timedelta(0) < last - first <= timedelta(days=31):
        raise HTTPException(422, "Request an increasing range of at most 31 days")
    return first, last


def validate_event(data, records):
    start, end = aware(data["starts_at"]), aware(data["ends_at"])
    timezone = ZoneInfo(ZONE_NAME)
    if start >= end or data["timezone"] != ZONE_NAME or any(
        stamp.utcoffset() != stamp.astimezone(timezone).utcoffset() for stamp in (start, end)
    ):
        raise HTTPException(422, "Expected an increasing interval in Asia/Kolkata")
    if data.get("transparency") != "transparent" and any(start < aware(event["ends_at"]) and end > aware(event["starts_at"]) for event in records):
        raise HTTPException(409, "Calendar conflict")


class CalendarProvider:
    def availability(self, window):
        records = self.list_events(window)["commitments"]
        start, end = window
        return {"occupied": [
            {"starts_at": max(start, aware(event["starts_at"])).isoformat(),
             "ends_at": min(end, aware(event["ends_at"])).isoformat()}
            for event in records
        ]}


class FixtureCalendarProvider(CalendarProvider):
    def __init__(self, store):
        self.store = store

    def list_events(self, window):
        records = self.store.list()
        if window:
            start, end = window
            records = [event for event in records if aware(event["starts_at"]) < end and aware(event["ends_at"]) > start]
        return {"commitments": records}

    def create_event(self, approval, data):
        if WINDOW_FIELDS & data.keys():
            raise HTTPException(422, "The fixture provider does not accept Google approval windows")
        return self.store.create(approval, data, validate_event)

    def get_event(self, event_id):
        return self.store.get(event_id)


def event_path(calendar_id, event_id=None):
    base = f"/calendars/{quote(calendar_id, safe='')}/events"
    return base if event_id is None else f"{base}/{quote(event_id, safe='')}"


def event_times(event, calendar_zone):
    dates = []
    try:
        for field in ("start", "end"):
            value = event[field]
            if "date" in value:
                stamp = datetime.combine(date.fromisoformat(value["date"]), time.min, ZoneInfo(calendar_zone))
            else:
                stamp = datetime.fromisoformat(value["dateTime"])
                if stamp.utcoffset() is None:
                    stamp = stamp.replace(tzinfo=ZoneInfo(value["timeZone"]))
            dates.append(stamp.astimezone(ZoneInfo(ZONE_NAME)))
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        raise GoogleError(502, "Google Calendar returned invalid event times.") from None
    if dates[0] >= dates[1]:
        raise GoogleError(502, "Google Calendar returned an invalid event interval.")
    return dates


def private_properties(event):
    return event.get("extendedProperties", {}).get("private", {})


def saved_event(event, calendar_zone=ZONE_NAME):
    start, end = event_times(event, calendar_zone)
    marker = private_properties(event).get("planBProposal")
    try:
        plan_b_event = marker and google_event_id(marker) == event["id"]
    except HTTPException:
        plan_b_event = False
    record = {"id": event["id"], "title": event.get("summary", "Busy") if plan_b_event else "Busy",
              "starts_at": start.isoformat(), "ends_at": end.isoformat(), "timezone": ZONE_NAME}
    if plan_b_event and event.get("transparency") == "transparent":
        record["transparency"] = "transparent"
    return record


class GoogleCalendarProvider(CalendarProvider):
    def __init__(self, connection=None):
        self.connection = connection or GoogleConnection()
        self.write_lock = Lock()

    def list_events(self, window, profile=None):
        if window is None:
            raise HTTPException(422, "Google Calendar needs an explicit start and end range")
        profile = profile or self.connection.profile()
        start, end = window
        records = []
        for calendar_id in profile["calendars"]:
            params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                      "showDeleted": "false", "maxResults": 2500,
                      "fields": "nextPageToken,timeZone,items(id,summary,start,end,status,transparency)"}
            seen = set()
            while True:
                page = self.connection.request(profile, "GET", event_path(calendar_id), params=params)
                for event in page.get("items", []):
                    if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
                        continue
                    first, last = event_times(event, page.get("timeZone", ZONE_NAME))
                    if first < end and start < last:
                        title = event.get("summary") if calendar_id == profile["calendar"] else None
                        records.append({"id": event["id"], "title": title or "Busy", "starts_at": first.isoformat(), "ends_at": last.isoformat()})
                page_token = page.get("nextPageToken")
                if not page_token:
                    break
                if page_token in seen:
                    raise GoogleError(502, "Google Calendar returned a repeated page token.")
                seen.add(page_token)
                params["pageToken"] = page_token
        records.sort(key=lambda event: (event["starts_at"], event["ends_at"], event["id"]))
        window_start, window_end = start.isoformat(), end.isoformat()
        revision = version([profile["connection_id"], profile["calendar"], profile["calendars"], window_start, window_end, records])
        return {"commitments": records, "google_window": {"window_start": window_start, "window_end": window_end, "revision": revision}}

    def get_event(self, event_id):
        profile = self.connection.profile()
        event = self.connection.request(profile, "GET", event_path(profile["calendar"], event_id),
                                        params={"fields": "id,summary,start,end,status,transparency,extendedProperties(private)"})
        if event.get("status") == "cancelled":
            raise GoogleError(404, "Calendar event was cancelled.")
        zone = ZONE_NAME
        if "date" in event.get("start", {}):
            metadata = self.connection.request(profile, "GET", event_path(profile["calendar"]),
                                               params={"fields": "timeZone", "maxResults": 1})
            zone = metadata["timeZone"]
        return saved_event(event, zone)

    def create_event(self, approval, data):
        verify_approval("calendar", approval, data)
        if approval["expires"] <= clock.time():
            raise HTTPException(403, "Approval expired")
        window = bounds(data.get("window_start"), data.get("window_end"))
        if window is None:
            raise HTTPException(422, "A reviewed Google Calendar range is required")
        start, end = aware(data["starts_at"]), aware(data["ends_at"])
        if not window[0] <= start < end <= window[1]:
            raise HTTPException(422, "Event is outside the reviewed Google Calendar range")
        details = {key: value for key, value in data.items() if key not in WINDOW_FIELDS}
        event_id = google_event_id(approval["proposal_id"])
        fingerprint = version(data)
        expected = {"id": event_id, **details}
        with self.write_lock:
            profile = self.connection.profile()
            path = event_path(profile["calendar"], event_id)

            def existing():
                try:
                    event = self.connection.request(profile, "GET", path, params={"fields": "id,summary,start,end,status,transparency,extendedProperties(private)"})
                except GoogleError as error:
                    if error.status == 404:
                        return None
                    raise
                if (event.get("status") == "cancelled" or private_properties(event).get("planBFingerprint") != fingerprint
                        or saved_event(event) != expected):
                    raise GoogleError(409, "Existing Google event does not match the approved proposal.")
                return expected

            if record := existing():
                return record
            fresh = self.list_events(window, profile)
            if fresh["google_window"]["revision"] != approval["snapshot"]:
                raise HTTPException(409, "Approved calendar state is stale")
            validate_event(details, fresh["commitments"])
            if start <= datetime.now(UTC):
                raise HTTPException(409, "Approved time is no longer in the future")
            if approval["expires"] <= clock.time():
                raise HTTPException(403, "Approval expired")
            body = {
                "id": event_id, "summary": details["title"], "visibility": "private",
                "start": {"dateTime": details["starts_at"], "timeZone": ZONE_NAME},
                "end": {"dateTime": details["ends_at"], "timeZone": ZONE_NAME},
                "extendedProperties": {"private": {"planBProposal": approval["proposal_id"], "planBFingerprint": fingerprint}},
            }
            if details.get("transparency") == "transparent":
                body["transparency"] = "transparent"
            try:
                saved = self.connection.request(profile, "POST", event_path(profile["calendar"]), json=body)
            except GoogleError as error:
                if error.status == 409 and (record := existing()):
                    return record
                raise
            if saved_event(saved) != expected:
                raise GoogleError(502, "Google returned unexpected event details. Check the event by ID before retrying.")
            return expected


def select_provider(store, google):
    mode = setting("PLAN_B_CALENDAR_PROVIDER", "fixture")
    if mode == "fixture":
        return FixtureCalendarProvider(store)
    if mode == "google":
        return google
    raise HTTPException(503, "Set PLAN_B_CALENDAR_PROVIDER to fixture or google")
