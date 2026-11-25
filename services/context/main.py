from zoneinfo import ZoneInfoNotFoundError

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from services.action_store import Store
from services.context.calendar_providers import GoogleCalendarProvider, bounds, select_provider
from services.context.google_oauth import GoogleError
from services.context.email_deadline import extract_deadline

app = FastAPI(title="Plan B Context Service")
google = GoogleCalendarProvider()
store = Store("calendar", [{
    "id": "cal-001", "title": "Design review",
    "starts_at": "2026-11-23T17:30:00+05:30", "ends_at": "2026-11-23T18:15:00+05:30",
}, {
    "id": "cal-002", "title": "Work incident review", "kind": "work", "protected": True,
    "starts_at": "2026-11-24T18:30:00+05:30", "ends_at": "2026-11-24T19:30:00+05:30",
}, {
    "id": "cal-003", "title": "Customer planning meeting", "kind": "work", "protected": True,
    "starts_at": "2026-11-25T19:00:00+05:30", "ends_at": "2026-11-25T20:00:00+05:30",
}, {
    "id": "cal-004", "title": "Family dinner", "kind": "family", "protected": True,
    "starts_at": "2026-11-26T18:30:00+05:30", "ends_at": "2026-11-26T20:00:00+05:30",
}, {
    "id": "cal-005", "title": "Volunteer shift", "kind": "personal", "protected": True,
    "starts_at": "2026-11-28T09:00:00+05:30", "ends_at": "2026-11-28T10:00:00+05:30",
}])


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    starts_at: str
    ends_at: str
    timezone: str
    proposal_id: str
    expires: int
    snapshot: str
    approval_signature: str
    window_start: str | None = None
    window_end: str | None = None
    transparency: str | None = Field(default=None, pattern="^transparent$")


@app.exception_handler(GoogleError)
def google_error(_, error):
    return JSONResponse(status_code=error.status, content={"detail": str(error)})


@app.get("/commitments", operation_id="list_commitments")
def list_commitments(start: str | None = None, end: str | None = None) -> dict:
    """List fixture commitments. For ranged Google Calendar reads use list_calendar_range."""
    return select_provider(store, google).list_events(bounds(start, end))


@app.get("/calendar/commitments/{start}/{end}", operation_id="list_calendar_range")
def list_calendar_range(start: str, end: str) -> dict:
    """List fixed calendar commitments between offset-aware RFC3339 timestamps."""
    return select_provider(store, google).list_events(bounds(start, end))


@app.get("/calendar/availability/{start}/{end}", operation_id="get_calendar_availability")
def get_calendar_availability(start: str, end: str) -> dict:
    """Return occupied intervals clipped to the requested offset-aware RFC3339 range."""
    return select_provider(store, google).availability(bounds(start, end))


@app.post("/calendar/events", operation_id="create_calendar_event")
def create_calendar_event(body: EventRequest) -> dict:
    """Create the exact calendar event authorized by an approved proposal."""
    fields = {"title", "starts_at", "ends_at", "timezone", "window_start", "window_end", "transparency"}
    return select_provider(store, google).create_event(body.model_dump(exclude=fields), body.model_dump(include=fields, exclude_none=True))


@app.get("/calendar/events/{id}", operation_id="get_calendar_event")
def get_calendar_event(id: str) -> dict:
    """Read a saved calendar event by its ID."""
    return select_provider(store, google).get_event(id)


class DeadlineMessage(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    reference: str
    timezone: str = "Asia/Kolkata"


@app.post("/email/deadline", operation_id="extract_email_deadline")
def extract_email_deadline(body: DeadlineMessage) -> dict:
    """Read-only: extract an explicit date and time from supplied email text and return the exact source quote."""
    try:
        return extract_deadline(body.message, body.reference, body.timezone)
    except (ValueError, ZoneInfoNotFoundError):
        return {"deadline": None, "reason": "A valid email reference timestamp with a timezone is required."}
