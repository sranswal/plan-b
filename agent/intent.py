"""Classify a conversational request. The model has no save permission."""

import re
from typing import Literal

import requests
from pydantic import BaseModel, ConfigDict, Field

from services.config import setting


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["focus", "routine", "activity", "task", "read_tasks", "read_calendar", "read_both", "clarify"]
    book: bool
    title: str = Field(max_length=200)
    deadline: str
    start: str
    end: str
    exact_start: bool
    clarification: str = Field(max_length=250)


def intent_prompt(question, now, timezone):
    return (
        f"Classify this Plan B request. Current planning time: {now.isoformat()}; user timezone: {timezone}. "
        "Use focus for an existing task, routine only for an explicitly requested weekly or recurring plan, "
        "and activity for one session with an explicitly requested duration, even if the activity is a configured routine. "
        "For multiple sessions of a named routine in a week, use routine and let the planner read the stored duration. "
        "'Book three gym sessions this week' is routine with book=true and needs no duration clarification. "
        "For one activity without a stated duration, clarify how much time to set aside. "
        "A bare statement of intent such as 'I need to go to the gym' never means a recurring plan or a booking. "
        "task for add/create a task or reminder, read_tasks, read_calendar, or read_both for listing, and clarify if the intent is unclear. "
        "Set book=true when the user asks to add, schedule, book, set a time for an activity, put on a calendar, create a task, or set a reminder. "
        "Find, show, suggest, and asking whether something fits are read-only unless the user also explicitly asks to save. "
        "Return deadline only when stated (an ISO date YYYY-MM-DD for date only or an offset-aware ISO datetime for a stated time). "
        "Use start/end for an explicitly requested scheduling window, offset-aware ISO datetime. Set exact_start for an exact appointment time. "
        "Recognize time preferences in natural language, including late morning (10–12), lunch (12–14), afternoon (14–17), and evening (17–21), in the user's timezone. "
        "If the user gives an updated time preference, it replaces the earlier time while keeping the activity, duration, and booking intent. Keep the original day unless the update names a different day. "
        "A clarification reply answers the preceding request. Combine hours and minutes, ignore conversational filler such as 'would be fine', and keep the original activity and day unless the reply changes them. A duration-only answer never changes whether the user asked to book. "
        "If a time preference is given without a day, apply it to the next occurrence unless an earlier day was specified. "
        "An update saying 'in the week' or 'during the week' changes a prior weekend preference to a weekday. "
        "For a stated day without a time, set start and end to that day's boundaries; never invent a requested time. "
        "If a stated scheduling constraint cannot be resolved, use clarify. Use empty strings for unused fields. "
        f"User request: {question}"
    )


def gemini_intent(prompt):
    key = setting("GEMINI_API_KEY")
    if not key:
        return None
    model = setting("PLAN_B_GEMINI_MODEL", "gemini-3.8-flash")
    if not re.fullmatch(r"gemini-[\w.-]+", model):
        raise RuntimeError("PLAN_B_GEMINI_MODEL must be a Gemini model ID.")
    try:
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key}, timeout=30,
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": Intent.model_json_schema()}},
        )
        if not response.ok:
            raise RuntimeError(f"Gemini intent routing failed (HTTP {response.status_code}). Check the key and model in .env.")
        parts = response.json()["candidates"][0]["content"]["parts"]
        output = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        return Intent.model_validate_json(output)
    except (requests.RequestException, ValueError, KeyError, IndexError):
        raise RuntimeError("Gemini did not return a usable intent.") from None
