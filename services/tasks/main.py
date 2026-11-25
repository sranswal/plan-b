from datetime import date

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from fastapi.responses import JSONResponse

from services.action_store import Store, aware
from services.config import setting
from services.context.google_oauth import GoogleError
from services.tasks.google_tasks import GoogleTasks

app = FastAPI(title="Plan B Tasks and Routines Service")
google_tasks = GoogleTasks()


@app.exception_handler(GoogleError)
def google_error(_, error):
    return JSONResponse(status_code=error.status, content={"detail": str(error)})


store = Store("task", [
    {
        "id": "task-101",
        "title": "Finish README for the Plan B hackathon submission",
        "priority": "high",
        "estimated_minutes": 90,
        "deadline": "2026-11-23T20:00:00+05:30",
        "status": "pending",
    },
    {
        "id": "task-102",
        "title": "Review notes for the next sprint",
        "priority": "medium",
        "estimated_minutes": 30,
        "deadline": "2026-11-24T11:00:00+05:30",
        "status": "pending",
    },
])


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    deadline: str | None = None
    proposal_id: str
    expires: int
    snapshot: str
    approval_signature: str
    source_quote: str | None = Field(default=None, max_length=1000)
    source_url: str | None = Field(default=None, max_length=1000)


@app.get("/tasks", operation_id="list_tasks")
def list_tasks() -> dict:
    """List pending tasks with priorities, effort in minutes, and deadlines."""
    if setting("PLAN_B_TASKS_PROVIDER") == "google":
        return {"tasks": google_tasks.list()}
    tasks = store.list()
    if deadline := setting("PLAN_B_LIVE_TASK_DEADLINE"):
        try:
            minutes = int(setting("PLAN_B_LIVE_TASK_MINUTES", "90"))
            if not 1 <= minutes <= 1440:
                raise ValueError
        except ValueError:
            raise HTTPException(503, "PLAN_B_LIVE_TASK_MINUTES must be an integer from 1 to 1440") from None
        title = "[Plan B test] Google Calendar integration check" if minutes == 15 else "[Plan B test] Hackathon README focus session"
        tasks.append({"id": "plan-b-live-test", "title": title,
                      "priority": "high", "estimated_minutes": minutes, "deadline": aware(deadline).isoformat(), "status": "pending"})
    return {"tasks": tasks}


def validate_task(data, records):
    if deadline := data["deadline"]:
        if len(deadline) == 10:
            try:
                date.fromisoformat(deadline)
            except ValueError:
                raise HTTPException(422, "Invalid due date") from None
        else:
            aware(deadline)


@app.post("/tasks", operation_id="create_task")
def create_task(body: TaskRequest) -> dict:
    """Create the exact task and offset-aware deadline authorized by an approved proposal."""
    fields = {"title", "deadline", "source_quote", "source_url"}
    data = {**body.model_dump(include=fields, exclude_none=True), "deadline": body.deadline,
            "status": "pending", "priority": "medium", "estimated_minutes": None}
    if setting("PLAN_B_TASKS_PROVIDER") == "google":
        return google_tasks.create(body.model_dump(exclude=fields), data)
    return store.create(body.model_dump(exclude=fields), data, validate_task)


@app.get("/tasks/{id}", operation_id="get_task")
def get_task(id: str) -> dict:
    """Read a saved task by its ID."""
    if setting("PLAN_B_TASKS_PROVIDER") == "google":
        return google_tasks.get(id)
    return store.get(id)


@app.get("/routines", operation_id="list_routines")
def list_routines() -> dict:
    """List flexible routines and their preferred times in Asia/Kolkata."""
    return {
        "routines": [
            {"id": "routine-1", "title": "Gym", "duration_minutes": 60, "preferences": "Evenings preferred; exact scheduling rules are not in this summary"},
            {"id": "routine-2", "title": "Walk", "duration_minutes": 25, "preferences": "Before breakfast"},
        ]
    }


@app.get("/routine-preferences", operation_id="get_routine_preferences")
def get_routine_preferences() -> dict:
    """Return detailed routine scheduling preferences: weekly frequency, duration, allowed days, time windows, and minimum rest between sessions."""
    return {
        "routine_preferences": [{
            "routine_id": "routine-1",
            "required_sessions_per_week": 3,
            "preferred_session_minutes": 60,
            "preferred_windows": [
                {"days": ["tue", "wed", "thu"], "start": "18:30", "end": "21:00"},
                {"days": ["sat"], "start": "09:00", "end": "12:00"},
            ],
            "allowed_days": ["tue", "wed", "thu", "sat"],
            "minimum_rest_minutes": 1440,
            "buffer_before_minutes": int(setting("PLAN_B_GYM_BUFFER_MINUTES", "0")),
            "buffer_after_minutes": int(setting("PLAN_B_GYM_BUFFER_MINUTES", "0")),
            "priority": "medium",
            "flexibility": "flexible",
            "can_move": True,
        }],
    }
