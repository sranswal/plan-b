"""Local browser coordinator for the Plan B judging demo."""

import asyncio
import json
import logging
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import re
import uuid
from datetime import date
from datetime import datetime, time as clock_time, timedelta
from contextlib import asynccontextmanager, closing
from pathlib import Path
from threading import Lock
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agent.actions import READ_TOOLS, Proposal, codex_trace, execute_approved, make_proposal, read_context, verify_execution
from agent.intent import Intent, gemini_intent, intent_prompt
from agent.schedule import collect_trace, find_slot, local_time, plan_from_trace, task_deadline, task_slot
from agent.weekly import catalog, plan_week, read_tools, routine_preference, weekly_from_trace
from services.config import PROJECT_ROOT, setting
from services.context.google_oauth import GoogleConnection
from services.context.email_deadline import ABSOLUTE_DATE
from services.tasks.google_tasks import TasksConnection
from services.action_store import version


SERVICES = ("context", "tasks")
EARLIEST = local_time("2026-11-23T17:00:00+05:30")
ASSETS = PROJECT_ROOT / "web"
COOKIE = "plan_b_browser"
SPOKEN_MINUTES = {"five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "twenty-five": 25,
                  "thirty": 30, "forty-five": 45, "sixty": 60, "ninety": 90}


class PlannerRequest(BaseModel):
    mode: Literal["auto", "focus", "routine", "activity", "email"] = "auto"
    question: str = Field(min_length=1, max_length=12000)
    subject: str = Field(default="", max_length=200)
    source_url: str = Field(default="", max_length=1000)
    reference: str = Field(default="", max_length=64)
    email_destination: Literal["task", "calendar"] = "task"
    timezone: str = Field(default="Asia/Kolkata", max_length=80)
    follow_up_to: str = Field(default="", max_length=1000)
    revise_time: bool = False


def generated(service):
    folder = PROJECT_ROOT / "services" / service
    return sorted(folder.glob("synapse_auto_server*.py"), key=lambda file: file.stat().st_mtime_ns)


def tool_calls(trace):
    return [{"server": item["server"], "tool": item["tool"], "status": item["status"]}
            for event in trace if event["type"] == "item.completed"
            and (item := event["item"])["type"] == "mcp_tool_call"]


def fixed_blocks(calendar):
    return [{"title": event["title"], "start": event["starts_at"], "end": event["ends_at"], "kind": "fixed"} for event in calendar]


HOUR_DURATION = r"(?:\d{1,2}(?:\.\d+)?|one|two|three|four)\s*(?:hours?|hrs?)\b"
MINUTE_DURATION = rf"(?:\d{{1,3}}|{'|'.join(SPOKEN_MINUTES)})\s*[- ]?\s*(?:minutes?|mins?)\b"
COMPOUND_DURATION = rf"({HOUR_DURATION})(?:\s*,\s*|\s+and\s+|\s+)({MINUTE_DURATION})"


def stated_minutes(question):
    def combine(match):
        total = sum(next(iter(stated_minutes(part)), 0) for part in match.groups())
        return f"{total} minutes" if all(stated_minutes(part) for part in match.groups()) else match.group()

    question = re.sub(COMPOUND_DURATION, combine, question, flags=re.I)
    numbers = {int(value) for value in re.findall(r"\b(\d{1,3})\s*[- ]?\s*(?:minutes?|mins?)\b", question, re.I)}
    for value in re.findall(r"\b(\d{1,2}(?:\.\d+)?|one|two|three|four)\s*(?:hours?|hrs?)\b", question, re.I):
        hours = {"one": 1, "two": 2, "three": 3, "four": 4}.get(value.lower())
        minutes = (hours if hours is not None else float(value)) * 60
        if minutes == int(minutes):
            numbers.add(int(minutes))
    for word, value in SPOKEN_MINUTES.items():
        # A hyphenated number is matched as a whole, rather than as its final word.
        if re.search(rf"(?<![\w-]){word}\s*[- ]?\s*(?:minutes?|mins?)\b", question, re.I):
            numbers.add(value)
    return numbers


def activity_slot(start, end, duration, busy, zone, explicit_clock_time=False):
    if explicit_clock_time:
        return find_slot(start, end, duration, busy, allow_end=True)
    day = start.astimezone(zone).date()
    last_day = end.astimezone(zone).date()
    while day <= last_day:
        opens = datetime.combine(day, clock_time(7), zone)
        closes = datetime.combine(day, clock_time(21), zone)
        lower, upper = max(start, opens), min(end, closes)
        if upper - lower >= duration:
            slot = find_slot(lower, upper, duration, busy, allow_end=True)
            if slot:
                return slot
        day += timedelta(days=1)
    return None


def user_zone(timezone):
    try:
        return ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        return EARLIEST.tzinfo


class Demo:
    def __init__(self, folder):
        self.key = secrets.token_hex(32)
        self.google = setting("PLAN_B_WEB_GOOGLE", "0") == "1"
        self.google_tasks = setting("PLAN_B_WEB_GOOGLE_TASKS", "0") == "1"
        tasks_profile = TasksConnection().profile() if self.google_tasks else None
        if self.google:
            profile = GoogleConnection().profile()
            selected = setting("PLAN_B_GOOGLE_CALENDAR_ID")
            if not selected or profile["calendar"] != selected or selected not in profile["calendars"]:
                raise RuntimeError("Browser Google mode requires the configured calendar to match the OAuth write calendar.")
        state = PROJECT_ROOT / ".plan-b" / "browser" if self.google or self.google_tasks else Path(folder)
        self.effort_db = state / "task-effort.sqlite3"
        self.effort_scope = tasks_profile["connection_id"] if tasks_profile else "fixture"
        self.env = {**os.environ, "PLAN_B_CALENDAR_PROVIDER": "google" if self.google else "fixture", "PLAN_B_STATE_DIR": str(state),
                    "PLAN_B_TASKS_PROVIDER": "google" if self.google_tasks else "fixture",
                    "PLAN_B_GYM_BUFFER_MINUTES": os.environ.get("PLAN_B_GYM_BUFFER_MINUTES", "15" if self.google else "0"),
                    "PLAN_B_LIVE_TASK_DEADLINE": "", "PLAN_B_APPROVAL_KEY": self.key}
        if tasks_profile:
            self.env["PLAN_B_GOOGLE_TASKS_CONNECTION_ID"] = tasks_profile["connection_id"]
        self.paths = {}
        for name, files in ((name, generated(name)) for name in SERVICES):
            override = os.environ.get(f"PLAN_B_WEB_{name.upper()}_SERVER")
            path = Path(override).resolve() if override else (files[-1] if files else None)
            if path is None or not path.is_file():
                raise RuntimeError(f"Generate the {name} MCP server with Synapse before starting the browser demo.")
            self.paths[name] = path
        self.processes = {}
        self.ports = {}
        self.pending = {}
        self.last_candidate = None
        self.booked = {}
        self.lock = Lock()
        self.session_key = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)

    def stop(self, name):
        if (proc := self.processes.pop(name, None)) and proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)

    def start(self, name):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", f"services.{name}.main:app",
                                 "--port", str(port), "--no-access-log", "--log-level", "warning"], cwd=PROJECT_ROOT, env=self.env)
        self.processes[name] = proc
        for _ in range(100):
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.2):
                    self.ports[name] = port
                    return
            except OSError:
                time.sleep(.1)
        raise RuntimeError(f"The {name} demo backend could not start on port {port}.")

    def catalogs(self):
        return {name: asyncio.run(catalog(name, path, ports=self.ports)) for name, path in self.paths.items()}

    def sources(self):
        return [{"name": name, "file": self.paths[name].name,
                 "tools": [{"name": tool.name, "description": tool.description} for tool in tools]}
                for name, tools in self.catalogs().items()]

    def earliest(self):
        return datetime.now(EARLIEST.tzinfo).replace(second=0, microsecond=0) + timedelta(minutes=5) if self.google else EARLIEST

    def calendar_range(self, earliest):
        return (earliest.isoformat(), (earliest + timedelta(days=7)).isoformat()) if self.google else None

    async def call(self, service, tool, arguments):
        params = StdioServerParameters(command=sys.executable, args=[str(self.paths[service])],
                                      env={"ENDPOINT_BASE_URL": f"http://127.0.0.1:{self.ports[service]}"})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
                if result.isError:
                    raise RuntimeError(f"{service}/{tool} failed. Check the generated Synapse server and provider connection.")
                return result.structuredContent or json.loads(result.content[0].text)

    def task_effort(self, tasks, change=None):
        self.effort_db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.effort_db)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS effort (scope TEXT, task TEXT, minutes INTEGER, PRIMARY KEY(scope, task))")
            if change:
                task_id, minutes = change
                if minutes is None:
                    db.execute("DELETE FROM effort WHERE scope=? AND task=?", (self.effort_scope, task_id))
                else:
                    db.execute("INSERT OR REPLACE INTO effort VALUES (?, ?, ?)", (self.effort_scope, task_id, minutes))
            overrides = dict(db.execute("SELECT task, minutes FROM effort WHERE scope=?", (self.effort_scope,)))
        return [{**task, "estimated_minutes": overrides.get(task["id"], task.get("estimated_minutes")),
                 "custom_effort": task["id"] in overrides} for task in tasks]

    def calendar_tools(self, window):
        if window:
            return {**READ_TOOLS, "context": ["list_calendar_range"]}, f"context/list_calendar_range with start={window[0]} and end={window[1]}"
        return READ_TOOLS, "context/list_commitments"

    def calendar(self, window):
        tool, args = ("list_calendar_range", dict(zip(("start", "end"), window))) if window else ("list_commitments", {})
        return fixed_blocks(asyncio.run(self.call("context", tool, args))["commitments"])

    def trace(self, allowed, prompt, schema=None):
        return codex_trace(self.paths, allowed, prompt, Path(__file__).with_name(schema) if schema else None, ports=self.ports)

    def classify(self, question, earliest, timezone):
        zone = user_zone(timezone)
        prompt = intent_prompt(question, earliest.astimezone(zone), zone.key)
        try:
            if intent := gemini_intent(prompt):
                return intent, "Gemini"
        except RuntimeError as error:
            logging.getLogger("uvicorn.error").warning("%s Falling back to Codex.", error)
        _, answer = collect_trace(self.trace({}, prompt, "intent-selection.schema.json"))
        return Intent.model_validate_json(answer), "Codex"

    def plan(self, request):
        original_request = request.follow_up_to
        if request.mode == "auto" and request.follow_up_to and request.revise_time:
            self.last_candidate = None
            request = request.model_copy(update={
                "question": f"{request.follow_up_to.strip()}\nUpdated time preference: {request.question}",
                "follow_up_to": "",
            })
        elif request.mode == "auto" and request.follow_up_to:
            reply = re.sub(r"\bminte\b", "minute", request.question, flags=re.I)
            request = request.model_copy(update={
                "question": f"{request.follow_up_to.strip()}\nClarification reply: {reply}",
                "follow_up_to": "",
            })
        if request.mode == "auto" and re.fullmatch(r"(?:yes[, ]+)?(?:book|schedule|save) (?:it|that|this)(?: please)?[.!]?", request.question.strip(), re.I):
            if self.last_candidate is None or self.last_candidate[0].expires <= time.time():
                return {"answer": "Find a slot first so I can show exactly what would be booked.", "blocks": None, "calls": [], "proposal": None}
            candidate = self.last_candidate
            self.last_candidate = None
            proposal = candidate[0]
            self.pending[proposal.id] = candidate
            return {"answer": "Review the exact slot before I book it.", "blocks": None, "calls": [],
                    "proposal": {"id": proposal.id, "action": proposal.action, "expires": proposal.expires, **proposal.data}}
        earliest = self.earliest()
        display_zone = user_zone(request.timezone)
        calendar_range = self.calendar_range(earliest)
        allowed, calendar_tool = self.calendar_tools(calendar_range)
        base_prompt = f"Use MCP only. Call {calendar_tool} and tasks/list_tasks before answering. "
        intent = None
        router = None
        mode = request.mode
        if mode == "auto":
            intent, router = self.classify(request.question, earliest, request.timezone)
            mode = intent.mode
            if mode == "routine" and not re.search(
                r"\b(?:weekly|recurring|each week|every week|per week|a week|"
                r"(?:two|three|four|five|six|seven|[2-9])\s+(?:[\w-]+\s+)?(?:sessions|times|workouts))\b",
                request.question, re.I,
            ):
                if stated_minutes(request.question):
                    mode = "activity"
                else:
                    intent.clarification = "How much time would you like to set aside for one session?"
                    mode = "clarify"
        booking = intent.book if intent else True
        if original_request and intent:
            booking = booking and bool(re.search(
                r"\b(?:book|schedule|set a time|put (?:it|this|that) on|add|create|set a reminder)\b",
                original_request, re.I,
            ))
        if mode in {"clarify", "read_calendar", "read_tasks", "read_both"}:
            response = self.inspect(mode, intent, calendar_range, router)
            if mode == "clarify":
                response["follow_up"] = request.question[:1000]
            return response
        proposal = None
        follow_up = None
        selected_task_id = None
        evidence = None
        extra_calls = []
        if mode == "routine":
            allowed = {**allowed, "tasks": read_tools(self.catalogs()["tasks"])}
            prompt = base_prompt + (
                "Get the routine name from the routine summary and use the most detailed available preferences tool for scheduling rules. Never infer missing rules. "
                "Return the routine_id from MCP evidence. A deterministic planner computes the schedule. "
            )
            trace = self.trace(allowed, prompt + f"User request: {request.question}", "routine-selection.schema.json")
        elif mode == "focus":
            prompt = base_prompt + "Return the task_id of the relevant pending task from MCP evidence. "
            trace = self.trace(allowed, prompt + f"User request: {request.question}", "task-selection.schema.json")
        elif mode == "activity":
            trace = self.trace(allowed, base_prompt + "Extract a short activity title and the explicitly requested duration in minutes. Use 0 for absent or uncertain duration. Return unsupported_time=true if the request contains a date, time window, or deadline other than an unrestricted next available slot. "
                               f"User request: {request.question}", "activity-selection.schema.json")
        elif mode == "task":
            trace = self.trace(allowed, base_prompt)
        else:
            relative = r"\b(?:by|due(?:\s+on)?|deadline(?:\s+is)?\s*:?)\s+(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
            if not request.reference and (re.search(relative, request.question, re.I) or not re.search(ABSOLUTE_DATE, request.question, re.I)):
                return {"answer": "Supply the message date and time for relative deadlines, or use a full date in the message.",
                        "blocks": self.calendar(calendar_range), "calls": [], "proposal": None}
            reference = request.reference or (earliest - timedelta(minutes=5)).isoformat()
            email = asyncio.run(self.call("context", "extract_email_deadline", {"message": request.question, "reference": reference,
                                                                                 "timezone": request.timezone}))
            extra_calls.append({"server": "context", "tool": "extract_email_deadline", "status": "completed"})
            if not email.get("deadline"):
                return {"answer": email["reason"], "blocks": self.calendar(calendar_range), "calls": extra_calls, "proposal": None}
            trace = self.trace(allowed, base_prompt)
            evidence = email["source_quote"]

        results, selection = collect_trace(trace)
        tasks, calendar, google_window = read_context(results, calendar_range)
        snapshot_tasks = tasks
        if mode in {"focus", "routine"}:
            tasks = self.task_effort(tasks)
        selected = json.loads(selection) if mode in {"routine", "focus", "activity"} else {}
        blocks = fixed_blocks(calendar)
        if mode == "routine":
            booked_ids = {event["id"] for event in calendar}
            scheduled = {task_id for event_id, task_id in self.booked.items() if event_id in booked_ids}
            unknown_effort = False
            if self.google:
                upcoming = [task for task in tasks if task.get("deadline") and earliest < task_deadline(task) < earliest + timedelta(days=7)]
                unknown_effort = any(not task.get("estimated_minutes") for task in upcoming)
                tasks = [task for task in upcoming if task.get("estimated_minutes")]
            rules = routine_preference(results, selected["routine_id"])
            if self.google and rules:
                answer = "No session fits the allowed windows."
            else:
                answer = weekly_from_trace(trace, earliest, scheduled_tasks=scheduled)
            if rules and rules.can_move and rules.flexibility == "flexible" and (self.google or answer.startswith("Yes.")):
                work, sessions = plan_week([task for task in tasks if task["id"] not in scheduled], calendar, rules, earliest)
                blocks.extend({"title": title, "start": start, "end": end, "kind": "focus"} for title, start, end in work)
                title = next((row["title"] for row in results.get(("tasks", "list_routines"), {}).get("routines", [])
                              if row["id"] == rules.routine_id), "Routine session")
                blocks.extend({"title": title, "start": start, "end": end, "kind": "routine"} for start, end in sessions)
                if sessions:
                    start, end = sessions[0]
                    proposal = self.calendar_proposal(title, start, end, snapshot_tasks, calendar, google_window)
                    if self.google:
                        answer = (f"Suggested {len(sessions)} of {rules.required_sessions_per_week} weekly sessions. None are booked yet. "
                                  + ("Review the first session to save it. " if booking else "") +
                                  f"Each block includes {rules.preferred_session_minutes} minutes for the routine, "
                                  f"{rules.buffer_before_minutes} before and {rules.buffer_after_minutes} after.")
                        if unknown_effort:
                            answer += " Some dated tasks have no estimate, so their work time is still unreserved."
        elif mode == "focus":
            if self.google or self.google_tasks or any(task["custom_effort"] for task in tasks):
                durations = stated_minutes(request.question)
                planning_tasks = [{**task, "estimated_minutes": next(iter(durations))} if len(durations) == 1 and
                                  task["id"] == selected["task_id"] else task for task in tasks]
                chosen, _, slot = task_slot(planning_tasks, calendar, selected["task_id"], earliest)
                if slot and (not google_window or slot[1] <= local_time(google_window["window_end"])):
                    proposal = self.calendar_proposal(chosen["title"], *slot, snapshot_tasks, calendar, google_window)
                    start, end = (point.astimezone(display_zone) for point in slot)
                    answer = f"Found a focus block on {start:%a %d %b, %H:%M}–{end:%H:%M}, before the selected task’s deadline."
                else:
                    answer = "No suitable focus block fits the reviewed window and task deadline."
            else:
                answer = plan_from_trace(trace, earliest)
                if answer.startswith("Suggested slot"):
                    proposal = make_proposal("calendar", selected, snapshot_tasks, calendar, earliest)
            selected_task_id = selected.get("task_id")
        elif mode == "activity":
            explicit_durations = stated_minutes(request.question)
            minutes = next(iter(explicit_durations)) if len(explicit_durations) == 1 else selected["minutes"]
            unresolved = (selected["unsupported_time"] or request.revise_time) and not (intent and (intent.start or intent.end))
            if (explicit_durations - {minutes}) or not 1 <= minutes <= 240:
                answer = ("How long should I set aside?" if minutes <= 0 or len(explicit_durations) > 1 else
                          "I can check up to four hours for one activity. How much time should I check?")
                follow_up = request.question
            elif unresolved:
                answer = "What day or time window should I check?"
                follow_up = request.question
            elif not selected["title"].strip():
                answer = "What would you like to make time for?"
                follow_up = request.question
            else:
                busy = [(local_time(event["starts_at"]), local_time(event["ends_at"])) for event in calendar]
                duration = timedelta(minutes=minutes)
                requested_start = local_time(intent.start) if intent and intent.start else earliest
                start_limit = max(earliest, requested_start)
                end_limit = min(earliest + timedelta(days=7), local_time(intent.end)) if intent and intent.end else earliest + timedelta(days=7)
                if intent and intent.exact_start:
                    end_limit = min(end_limit, requested_start + duration)
                explicit_clock_time = bool(intent and intent.exact_start) or bool(re.search(
                    r"\b\d{1,2}(?::\d{2}\b|\s*(?:am|pm)\b)|\b(?:noon|midnight|overnight)\b", request.question, re.I))
                slot = activity_slot(start_limit, end_limit, duration, busy, display_zone, explicit_clock_time)
                answer = ("No slot fits the requested window." if explicit_clock_time else
                          "No slot fits between 07:00 and 21:00 in the requested window. You can ask for a specific time outside those hours.")
                if slot:
                    proposal = self.calendar_proposal(selected["title"], *slot, tasks, calendar, google_window)
                    start, end = (point.astimezone(display_zone) for point in slot)
                    answer = f"{selected['title']} · {start:%a %d %b, %I:%M %p}–{end:%I:%M %p}"
        elif mode == "task":
            if not booking or not intent.title.strip():
                answer = "Tell me what task or reminder you want to add."
            else:
                deadline = None
                if intent.deadline:
                    if len(intent.deadline) == 10:
                        deadline = date.fromisoformat(intent.deadline).isoformat()
                    else:
                        deadline = local_time(intent.deadline).astimezone(display_zone).isoformat()
                data = {"title": intent.title.strip(), "deadline": deadline, "status": "pending",
                        "priority": "medium", "estimated_minutes": None}
                proposal = Proposal(str(uuid.uuid4()), "task", int(time.time()) + 300, version(calendar), version(tasks), data)
                answer = "Review the task before saving." + (" Google Tasks stores the date; the exact deadline is included in its notes." if self.google_tasks and deadline else "")
        else:
            title = request.subject.strip() or "Email deadline"
            stable = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([request.source_url, title, email["deadline"], evidence])))
            if request.email_destination == "calendar":
                start = local_time(email["deadline"])
                end = start + timedelta(minutes=1)
                if not self.google:
                    answer = "Enable browser Google mode to save a non-blocking deadline marker. You can save the task in fixture mode."
                elif start < earliest or end > local_time(google_window["window_end"]):
                    answer = "This deadline falls outside the reviewed Google Calendar window. Save it as a task instead."
                else:
                    candidate = self.calendar_proposal(f"Deadline: {title}"[:200], start, end, tasks, calendar, google_window)
                    marker_id = str(uuid.uuid5(uuid.UUID(stable), "calendar-marker"))
                    proposal = Proposal(marker_id, "calendar", candidate.expires, candidate.calendar_version, candidate.tasks_version,
                                        {**candidate.data, "transparency": "transparent"}, google_window)
                    answer = "Review the one-minute deadline marker. It will not block your availability or estimate your work."
            elif any(task["id"] == stable for task in tasks):
                answer = "This email deadline is already in Plan B Tasks."
            else:
                data = {"title": title, "deadline": email["deadline"], "estimated_minutes": None, "priority": "medium",
                        "status": "pending", "source_quote": evidence}
                if request.source_url.startswith("https://mail.google.com/"):
                    data["source_url"] = request.source_url
                proposal = Proposal(stable, "task", int(time.time()) + 300, version(calendar), version(tasks), data)
                answer = "Found an explicit deadline. Work duration stays blank. Review it to save."

        if proposal:
            candidate = (proposal, selected_task_id, calendar_range)
            if booking:
                self.pending = {item_id: item for item_id, item in self.pending.items() if item[0].expires > time.time()}
                self.pending[proposal.id] = candidate
            elif proposal.action == "calendar":
                self.last_candidate = candidate
            if proposal.action == "calendar" and mode != "routine":
                blocks.append({"title": proposal.data["title"], "start": proposal.data["starts_at"],
                               "end": proposal.data["ends_at"], "kind": "focus"})

        return {"answer": answer, "blocks": blocks, "router": router, "evidence": evidence, "calls": extra_calls + tool_calls(trace),
                "time_revision": request.question[:1000] if mode == "activity" and (proposal or request.revise_time) else None,
                "follow_up": follow_up[:1000] if follow_up else None, "proposal":
                {"id": proposal.id, "action": proposal.action, "expires": proposal.expires, **proposal.data} if proposal and booking else None}

    def inspect(self, mode, intent, window, router):
        tool_calls = []
        blocks = None
        answers = []
        if mode in {"read_calendar", "read_both"}:
            blocks = self.calendar(window)
            tool_calls.append({"server": "context", "tool": "list_calendar_range" if window else "list_commitments", "status": "completed"})
            answers.append(f"{len(blocks)} calendar commitments in the reviewed window.")
        if mode in {"read_tasks", "read_both"}:
            result = asyncio.run(self.call("tasks", "list_tasks", {}))
            tasks = result["tasks"]
            tool_calls.append({"server": "tasks", "tool": "list_tasks", "status": "completed"})
            answers.append(f"{len(tasks)} pending tasks" + (":\n" + "\n".join(f"• {task['title']}" for task in tasks[:15]) if tasks else "."))
        return {"answer": "\n".join(answers) or intent.clarification or "What would you like me to find, book, or add?",
                "router": router, "blocks": blocks, "calls": tool_calls, "proposal": None}

    @staticmethod
    def calendar_proposal(title, start, end, tasks, calendar, window):
        data = {"title": title, "starts_at": start.isoformat(), "ends_at": end.isoformat(), "timezone": "Asia/Kolkata"}
        if window:
            data.update({key: window[key] for key in ("window_start", "window_end")})
        return Proposal(str(uuid.uuid4()), "calendar", int(time.time()) + 300, version(calendar), version(tasks), data, window)


@asynccontextmanager
async def lifespan(app):
    with tempfile.TemporaryDirectory(prefix="plan-b-browser-") as folder:
        demo = Demo(folder)
        app.state.demo = demo
        try:
            for name in SERVICES:
                demo.start(name)
            yield
        finally:
            for name in SERVICES:
                demo.stop(name)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")


def get_demo(request, *, write=False):
    demo = request.app.state.demo
    if request.cookies.get(COOKIE) != demo.session_key or (write and request.headers.get("X-Plan-B-Action") != demo.csrf):
        raise HTTPException(403, "Open the local Plan B page first.")
    return demo


@app.get("/")
def index(request: Request):
    demo = request.app.state.demo
    response = FileResponse(ASSETS / "index.html", headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})
    response.set_cookie(COOKIE, demo.session_key, httponly=True, samesite="strict")
    return response


@app.get("/api/catalog")
def sources(request: Request, response: Response, timezone: str = "Asia/Kolkata"):
    demo = get_demo(request)
    response.headers["Cache-Control"] = "no-store"
    with demo.lock:
        earliest = demo.earliest()
        if demo.google:
            earliest = earliest.astimezone(user_zone(timezone))
        monday = (earliest - timedelta(days=earliest.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        preview = (monday.isoformat(), (monday + timedelta(days=21)).isoformat()) if demo.google else None
        return {"sources": demo.sources(), "actionToken": demo.csrf, "blocks": demo.calendar(preview),
                "provider": "google" if demo.google else "fixture", "tasksProvider": "google" if demo.google_tasks else "fixture",
                "date": monday.date().isoformat()}


class TaskEffortRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=500)
    minutes: int | None = Field(ge=1, le=1440)


@app.get("/api/tasks")
def dashboard_tasks(request: Request):
    demo = get_demo(request)
    with demo.lock:
        tasks = asyncio.run(demo.call("tasks", "list_tasks", {}))["tasks"]
        return demo.task_effort(tasks)


@app.post("/api/tasks/effort")
def update_task_effort(change: TaskEffortRequest, request: Request):
    demo = get_demo(request, write=True)
    with demo.lock:
        tasks = asyncio.run(demo.call("tasks", "list_tasks", {}))["tasks"]
        if not any(task["id"] == change.task_id for task in tasks):
            raise HTTPException(404, "This task is no longer in the connected pending list.")
        updated = demo.task_effort(tasks, (change.task_id, change.minutes))
        demo.pending.clear()
        demo.last_candidate = None
        return updated


@app.post("/api/refresh")
def refresh(request: Request):
    demo = get_demo(request, write=True)
    with demo.lock:
        if not (files := generated("tasks")):
            raise HTTPException(409, "Generate the Tasks server with Synapse first.")
        demo.stop("tasks")
        demo.start("tasks")
        demo.paths["tasks"] = files[-1]
        return {"sources": demo.sources()}


@app.post("/api/plan")
def plan(question: PlannerRequest, request: Request):
    demo = get_demo(request, write=True)
    try:
        with demo.lock:
            return demo.plan(question)
    except (ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise HTTPException(503, str(error) if not isinstance(error, subprocess.TimeoutExpired) else "Codex took too long to respond.") from None


@app.post("/api/proposals/{proposal_id}/{decision}")
def decide(proposal_id: str, decision: Literal["approve", "decline"], request: Request):
    demo = get_demo(request, write=True)
    with demo.lock:
        return decide_proposal(demo, proposal_id, decision)


def gmail_demo(request):
    extension_id = setting("PLAN_B_GMAIL_EXTENSION_ID", "")
    if not re.fullmatch(r"[a-p]{32}", extension_id) or request.headers.get("origin") != f"chrome-extension://{extension_id}":
        raise HTTPException(403, "Pair the Plan B extension with the local server first.")
    return request.app.state.demo


class GmailMessage(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    subject: str = Field(default="", max_length=200)
    source: str = Field(pattern=r"^https://mail\.google\.com/", max_length=1000)
    reference: str = Field(default="", max_length=64)
    timezone: str = Field(max_length=80)


@app.post("/api/gmail/preview")
def preview_gmail(message: GmailMessage, request: Request):
    demo = gmail_demo(request)
    if not demo.google:
        raise HTTPException(409, "Connect Google Calendar to add deadlines.")
    zone = user_zone(message.timezone)
    now = datetime.now(zone)
    if not message.reference and re.search(r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", message.message, re.I):
        return {}
    with demo.lock:
        email = asyncio.run(demo.call("context", "extract_email_deadline", {
            "message": message.message, "reference": message.reference or now.isoformat(), "timezone": zone.key,
        }))
        if not email.get("deadline"):
            return {}
        start = local_time(email["deadline"])
        end = start + timedelta(minutes=1)
        if start <= now or end > now + timedelta(days=7):
            return {}
        window = (now.replace(microsecond=0).isoformat(), (now + timedelta(days=7)).replace(microsecond=0).isoformat())
        events = asyncio.run(demo.call("context", "list_calendar_range", dict(zip(("start", "end"), window))))
        tasks = asyncio.run(demo.call("tasks", "list_tasks", {}))["tasks"]
        title = f"Deadline: {message.subject.strip() or 'Email deadline'}"[:200]
        candidate = demo.calendar_proposal(title, start, end, tasks, events["commitments"], events["google_window"])
        stable = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["gmail", message.source, title, email["deadline"]])))
        approval = Proposal(stable, "calendar", candidate.expires, candidate.calendar_version, candidate.tasks_version,
                            {**candidate.data, "transparency": "transparent"}, candidate.google_window)
        demo.pending = {key: item for key, item in demo.pending.items() if item[0].expires > time.time()}
        token = secrets.token_urlsafe(32)
        demo.pending[token] = (approval, None, window)
        return {"token": token, "title": title, "deadline": start.isoformat(), "quote": email["source_quote"]}


@app.post("/api/gmail/{token}/approve")
def approve_gmail(token: str, request: Request):
    demo = gmail_demo(request)
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(404, "Deadline preview was not found.")
    with demo.lock:
        return {"status": decide_proposal(demo, token, "approve")["status"]}


def decide_proposal(demo, proposal_id, decision):
    pending = demo.pending.pop(proposal_id, None)
    if pending is None:
        raise HTTPException(409, "This proposal has expired or was already handled.")
    proposal, task_id, calendar_range = pending
    if decision == "decline":
        return {"status": "declined", "calls": []}
    confirmation = f"approve {proposal.id}"
    try:
        proposal.require_confirmation(confirmation)
        allowed, calendar_tool = demo.calendar_tools(calendar_range)
        recheck = demo.trace(allowed, f"Use MCP only. Call {calendar_tool} and tasks/list_tasks once to recheck the approved proposal.")
        results, _ = collect_trace(recheck)
        tasks, calendar, window = read_context(results, calendar_range)
        payload = proposal.authorize(confirmation, tasks, calendar, demo.key, window if proposal.action == "calendar" else None)
    except (ValueError, RuntimeError, subprocess.TimeoutExpired):
        raise HTTPException(409, "Proposal was stale, expired, or could not be rechecked. No write was attempted.") from None
    try:
        trace = execute_approved(demo.paths, proposal, payload, ports=demo.ports)
        verified = verify_execution(trace, proposal)
    except (ValueError, RuntimeError, subprocess.TimeoutExpired):
        return {"status": "unverified", "id": proposal.result_id, "calls": tool_calls(recheck)}
    if verified and task_id:
        demo.booked[proposal.result_id] = task_id
    saved_id = proposal.result_id
    if verified and proposal.action == "task" and demo.google_tasks:
        results, _ = collect_trace(trace)
        saved_id = results[("tasks", "get_task")]["provider_id"]
    return {"status": "verified" if verified else "unverified", "id": saved_id,
            "calls": tool_calls(recheck) + tool_calls(trace)}
