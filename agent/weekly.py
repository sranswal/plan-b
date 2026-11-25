"""Discover read tools and validate a weekly routine plan from their MCP results."""

import argparse
import asyncio
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Literal

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field

from agent.actions import MCP_PORTS, codex_trace
from agent.schedule import collect_trace, find_slot, local_time, schedule_inputs, task_deadline, task_slot


Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class Window(BaseModel):
    days: list[Day] = Field(min_length=1)
    start: time
    end: time


class RoutinePreference(BaseModel):
    routine_id: str
    required_sessions_per_week: int = Field(ge=1, le=7)
    preferred_session_minutes: int = Field(ge=1, le=1440)
    preferred_windows: list[Window] = Field(min_length=1)
    allowed_days: list[Day] = Field(min_length=1)
    minimum_rest_minutes: int = Field(ge=0)
    buffer_before_minutes: int = Field(default=0, ge=0, le=180)
    buffer_after_minutes: int = Field(default=0, ge=0, le=180)
    priority: str
    flexibility: Literal["flexible", "fixed"]
    can_move: bool


async def catalog(name, path, *, ports=None):
    params = StdioServerParameters(
        command=sys.executable, args=[str(path)],
        env={"ENDPOINT_BASE_URL": f"http://127.0.0.1:{(ports or MCP_PORTS)[name]}"},
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = []
            page = await session.list_tools()
            while True:
                tools.extend(page.tools)
                if page.nextCursor is None:
                    return tools
                page = await session.list_tools(cursor=page.nextCursor)


def read_tools(tools):
    # The trusted local Synapse builds use list_/get_ exclusively for GET routes.
    return [tool.name for tool in tools if tool.name.startswith(("list_", "get_")) and not tool.inputSchema.get("properties")]


def plan_week(tasks, calendar, preference, earliest):
    monday = earliest.date() - timedelta(days=earliest.weekday())
    week_end = datetime.combine(monday + timedelta(days=7), time.min, earliest.tzinfo)
    occupied = list(calendar)
    reservations = []
    pending = sorted(
        (task for task in tasks if task["status"] == "pending" and task.get("deadline") and task_deadline(task) <= week_end),
        key=task_deadline,
    )
    for task in pending:
        if not task.get("estimated_minutes"):
            raise ValueError(f'Cannot protect deadline for "{task["title"]}" without an effort estimate.')
        _, deadline, slot = task_slot(tasks, occupied, task["id"], earliest)
        if slot is None:
            raise ValueError(f'Could not reserve work for "{task["title"]}" before its {deadline:%a %H:%M} IST deadline.')
        start, end = slot
        occupied.append({"starts_at": start.isoformat(), "ends_at": end.isoformat()})
        reservations.append((task["title"], start, end))

    duration = timedelta(minutes=preference.preferred_session_minutes + preference.buffer_before_minutes + preference.buffer_after_minutes)
    rest = timedelta(minutes=preference.minimum_rest_minutes)
    busy = [(local_time(event["starts_at"]), local_time(event["ends_at"])) for event in occupied]
    windows = []
    for index, day in enumerate(DAYS):
        if day not in preference.allowed_days:
            continue
        date = monday + timedelta(days=index)
        for window in preference.preferred_windows:
            if day in window.days:
                if window.start.tzinfo or window.end.tzinfo or window.start >= window.end:
                    raise ValueError("Routine windows must be increasing local times within one day.")
                windows.append((datetime.combine(date, window.start, earliest.tzinfo), datetime.combine(date, window.end, earliest.tzinfo)))

    sessions = []
    next_start = earliest
    while len(sessions) < preference.required_sessions_per_week:
        fits = [find_slot(max(start, next_start), end, duration, busy, allow_end=True) for start, end in windows]
        slot = min((fit for fit in fits if fit is not None), default=None)
        if slot is None:
            break
        sessions.append(slot)
        next_start = slot[1] + rest
    return reservations, sessions


def routine_preference(results, routine_id):
    preferences = [
        item for (server, _), result in results.items() if server == "tasks"
        for item in result.get("routine_preferences", []) if item["routine_id"] == routine_id
    ]
    return RoutinePreference.model_validate(preferences[0]) if preferences else None


def weekly_from_trace(trace, earliest, *, scheduled_tasks=()):
    results, selection = collect_trace(trace)
    tasks, calendar = schedule_inputs(results)
    routine_id = json.loads(selection or "{}").get("routine_id")
    preference = routine_preference(results, routine_id)
    if preference is None:
        return "I can see the task deadlines and calendar commitments. I can't validate a weekly routine plan without structured frequency, allowed days, time windows, and spacing rules."
    if preference.flexibility != "flexible" or not preference.can_move:
        return "This routine is fixed. I need its existing session times to assess changes."
    try:
        reservations, sessions = plan_week([task for task in tasks if task["id"] not in scheduled_tasks], calendar, preference, earliest)
    except ValueError as error:
        return f"I couldn't validate a weekly plan. {error}"
    count = preference.required_sessions_per_week
    if len(sessions) < count:
        return f"I can only fit {len(sessions)} of {count} required sessions after protecting deadlines and commitments within the allowed days, preferred windows, and minimum spacing."
    lines = [f"Yes. {count} sessions fit in Asia/Kolkata (IST) without overlapping commitments or reserved deadline work."]
    lines += [f"  {start:%a %b %d, %H:%M}–{end:%H:%M}" for start, end in sessions]
    lines += [f"Reserved work for {title}: {start:%a %b %d, %H:%M}–{end:%H:%M} IST." for title, start, end in reservations]
    lines.append(f"Each session takes {preference.preferred_session_minutes} minutes with at least {preference.minimum_rest_minutes} minutes of rest between sessions.")
    if preference.buffer_before_minutes or preference.buffer_after_minutes:
        lines.append(f"Each calendar block also includes {preference.buffer_before_minutes} minutes before and {preference.buffer_after_minutes} minutes after the session.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--earliest", required=True, type=local_time)
    parser.add_argument("--request", required=True)
    for server in MCP_PORTS:
        parser.add_argument(f"--{server}", type=Path, required=True)
    args = parser.parse_args()
    paths = {server: getattr(args, server) for server in MCP_PORTS}
    allowed = {}
    for server, path in paths.items():
        tools = asyncio.run(catalog(server, path))
        allowed[server] = read_tools(tools)
        print(f"{server} catalog: {', '.join(tool.name for tool in tools)}", flush=True)
    prompt = (
        "Gather deadlines from the tasks server and fixed commitments from the context server. "
        "Inspect the available read tools and call the most detailed relevant source for routine scheduling rules. "
        "If no detailed source is available, use the routine summary to identify the routine. Never infer absent rules. "
        "Return the routine_id from tool evidence, or an empty routine_id if unknown. A deterministic planner will compute the schedule. "
        f"The week contains {args.earliest.date()}, in Asia/Kolkata. Use only MCP. User request: {args.request}"
    )
    trace = codex_trace(paths, allowed, prompt, Path(__file__).with_name("routine-selection.schema.json"))
    _, selection = collect_trace(trace)
    print(f"Codex selection: {selection}")
    print(weekly_from_trace(trace, args.earliest))


if __name__ == "__main__":
    main()
