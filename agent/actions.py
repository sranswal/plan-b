"""Trusted local coordinator for proposing and approving one fictional write."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.schedule import collect_trace, local_time, schedule_inputs, task_slot
from services.action_store import signature, version
from services.config import setting
from services.context.calendar_providers import WINDOW_FIELDS, google_event_id


READ_TOOLS = {"context": ["list_commitments"], "tasks": ["list_tasks"]}
WRITE_TOOLS = {
    "calendar": ("context", "create_calendar_event", "get_calendar_event"),
    "task": ("tasks", "create_task", "get_task"),
}
MCP_PORTS = {"context": 8000, "tasks": 8001}


@dataclass(frozen=True)
class Proposal:
    id: str
    action: str
    expires: int
    calendar_version: str
    tasks_version: str
    data: dict
    google_window: dict | None = None

    @property
    def result_id(self):
        return google_event_id(self.id) if self.google_window else self.id

    def require_confirmation(self, confirmation):
        if confirmation != f"approve {self.id}":
            raise ValueError("Proposal declined or approval did not match. No write was enabled.")
        if time.time() >= self.expires:
            raise ValueError("Proposal expired. No write was enabled.")

    def authorize(self, confirmation, tasks, calendar, secret, google_window=None):
        self.require_confirmation(confirmation)
        if (version(tasks) != self.tasks_version or version(calendar) != self.calendar_version
                or google_window != self.google_window):
            raise ValueError("Proposal is stale. Task or calendar state changed. No write was enabled.")
        if len(secret) < 32:
            raise ValueError("Set PLAN_B_APPROVAL_KEY in the trusted launcher and both backends.")
        snapshot = self.calendar_version if self.action == "calendar" else self.tasks_version
        if google_window:
            snapshot = google_window["revision"]
        fields = {"title", "deadline", "starts_at", "ends_at", "timezone", "transparency", "source_quote", "source_url"} | WINDOW_FIELDS
        return {
            **{key: value for key, value in self.data.items() if key in fields},
            "proposal_id": self.id,
            "expires": self.expires,
            "snapshot": snapshot,
            "approval_signature": signature(secret, self.action, self.id, self.expires, snapshot, self.data),
        }


def make_proposal(action, selection, tasks, calendar, earliest, google_window=None, live_test=False):
    if action == "calendar":
        task, _, slot = task_slot(tasks, calendar, selection["task_id"], earliest)
        if slot is None:
            raise ValueError("No feasible calendar slot before the task deadline. No proposal was created.")
        start, end = slot
        data = {"title": task["title"], "starts_at": start.isoformat(), "ends_at": end.isoformat(), "timezone": "Asia/Kolkata"}
        if google_window:
            if end > local_time(google_window["window_end"]):
                raise ValueError("The proposed slot falls outside the reviewed Google Calendar range.")
            if live_test and (task["id"] != "plan-b-live-test" or not task["title"].startswith("[Plan B test]")):
                raise ValueError("The opt-in live test requires the labeled test task from Tasks MCP.")
            data.update({field: google_window[field] for field in WINDOW_FIELDS})
    else:
        title = selection["title"].strip()
        deadline = datetime.fromisoformat(selection["deadline"])
        if not title or len(title) > 200 or deadline.utcoffset() is None or deadline <= earliest:
            raise ValueError("A title and future offset-aware deadline are required")
        data = {"title": title, "deadline": deadline.isoformat(), "status": "pending", "priority": "medium", "estimated_minutes": None}
    return Proposal(str(uuid.uuid4()), action, int(time.time()) + 300, version(calendar), version(tasks), data, google_window)


def codex_executable():
    if configured := setting("PLAN_B_CODEX_EXE"):
        executable = Path(configured).expanduser()
        if not executable.is_file():
            raise RuntimeError("PLAN_B_CODEX_EXE must point to the Codex executable.")
        return str(executable.resolve())
    if executable := shutil.which("codex.exe" if os.name == "nt" else "codex"):
        return executable
    if os.name == "nt" and (local_app_data := os.environ.get("LOCALAPPDATA")):
        desktop_bin = Path(local_app_data) / "OpenAI/Codex/bin"
        if executable := max(desktop_bin.glob("*/codex.exe"), key=lambda path: path.stat().st_mtime, default=None):
            return str(executable)
    raise RuntimeError("Codex CLI was not found. Add it to PATH or set PLAN_B_CODEX_EXE to the full executable path.")


def codex_trace(paths, allowed, prompt, schema=None, *, ports=None):
    args = [codex_executable(), "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--sandbox", "read-only", "--json"]
    for name, tools in allowed.items():
        port = (ports or MCP_PORTS)[name]
        policies = ",".join(f'{tool}={{approval_mode="approve"}}' for tool in tools)
        args += ["-c", (
            f'mcp_servers.{name}={{command={json.dumps(sys.executable)},args={json.dumps([str(paths[name])])},'
            f'env={{ENDPOINT_BASE_URL="http://127.0.0.1:{port}"}},required=true,enabled_tools={json.dumps(tools)},tools={{{policies}}}}}'
        )]
    if schema:
        args += ["--output-schema", str(schema)]
    env = os.environ.copy()
    for key in ("PLAN_B_APPROVAL_KEY", "PLAN_B_GOOGLE_CLIENT_FILE", "PLAN_B_GOOGLE_CALENDAR_ID", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        env.pop(key, None)
    try:
        output = subprocess.run([*args, "-"], input=prompt, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
    except OSError as error:
        raise RuntimeError(f"Could not start the Codex CLI: {error.strerror}") from error
    if output.returncode:
        raise RuntimeError(f"Codex CLI exited with status {output.returncode}")
    trace = [json.loads(line) for line in output.stdout.splitlines() if line.strip()]
    for entry in trace:
        item = entry.get("item", {})
        if entry["type"] == "item.completed" and item.get("type") == "mcp_tool_call":
            failure = [str((item.get("error") or {}).get("message", ""))]
            if item["status"] == "failed":
                failure.extend(part.get("text", "") for part in (item.get("result") or {}).get("content", []))
            http = re.search(r"\b([45]\d{2}) (?:Client|Server) Error\b", " ".join(failure))
            suffix = f" (HTTP {http[1]})" if http else ""
            print(f'MCP {item["server"]}/{item["tool"]}: {item["status"]}{suffix}', flush=True)
    return trace


def verify_execution(trace, proposal):
    results, _ = collect_trace(trace)
    server, write, read = WRITE_TOOLS[proposal.action]
    expected = {"id": proposal.result_id, **{key: value for key, value in proposal.data.items() if key not in WINDOW_FIELDS}}
    def matches(result):
        return result is not None and all(key in result and result[key] == value for key, value in expected.items())

    write_confirmed = matches(results.get((server, write)))
    receipt = {key: value for key, value in expected.items() if key not in {"source_quote", "source_url"}
               and (not proposal.google_window or key != "title")}
    if write_confirmed:
        print("Write response:", json.dumps(receipt, ensure_ascii=False))
    if matches(results.get((server, read))):
        print("Verified saved record:", json.dumps(receipt, ensure_ascii=False))
        return True
    status = "Write response received" if write_confirmed else "Write outcome unknown"
    print(f"{status}. Read-back did not verify record {proposal.result_id}. No retry was issued.")
    return False


def execute_approved(paths, proposal, payload, *, ports=None):
    server, write, read = WRITE_TOOLS[proposal.action]
    prompt = (
        f"Use MCP only. Call {server}/{write} at most once with these exact arguments: {json.dumps(payload)}. "
        f"Then call {server}/{read} with id {proposal.result_id} to verify the persisted record, even if the write response was unclear. "
        "Never repeat the write. Report whether the saved record was found. Do not echo approval arguments."
    )
    return codex_trace(paths, {server: [write, read]}, prompt, ports=ports)


def read_context(results, calendar_range):
    if not calendar_range:
        return (*schedule_inputs(results), None)
    view = results.get(("context", "list_calendar_range"), {})
    window = view.get("google_window", {})
    expected_start, expected_end = calendar_range
    if (window.get("window_start") != expected_start or window.get("window_end") != expected_end
            or not window.get("revision")):
        raise ValueError("Could not verify the requested live Google Calendar range. Check the provider, OAuth connection, and generated MCP path.")
    tasks, calendar = schedule_inputs({**results, ("context", "list_commitments"): view})
    return tasks, calendar, window


def run_action(paths, action, request, earliest, confirm, *, google=False, live_test=False):
    if live_test and not google:
        raise ValueError("The labeled live test requires Google Calendar mode.")
    calendar_range = None
    read_tools = READ_TOOLS
    calendar_call = "context/list_commitments"
    if google:
        if action != "calendar" or earliest <= datetime.now(UTC):
            raise ValueError("Google Calendar actions require a future planning start and the calendar workflow.")
        calendar_range = (earliest.isoformat(), (earliest + timedelta(days=7)).isoformat())
        read_tools = {**READ_TOOLS, "context": ["list_calendar_range"]}
        calendar_call = f"context/list_calendar_range with start={calendar_range[0]} and end={calendar_range[1]}"
    preview_prompt = (
        f"Call {calendar_call} and tasks/list_tasks. Use MCP only. Return JSON for this read-only proposal phase. "
        "For calendar select the relevant pending task_id. For task creation extract title and an exact ISO 8601 deadline "
        "with the intended UTC offset. Leave unused fields empty. If ambiguous leave the relevant field empty. "
        f"The planning date is {earliest.date()} and the default timezone is Asia/Kolkata. "
        f"Requested workflow: {action}. User request: {request}"
    )
    results, selection = collect_trace(codex_trace(paths, read_tools, preview_prompt, Path(__file__).with_name("action-selection.schema.json")))
    tasks, calendar, window = read_context(results, calendar_range)
    proposal = make_proposal(action, json.loads(selection), tasks, calendar, earliest, window, live_test)
    target = "selected Google calendar" if google else action
    print(f"Proposal {proposal.id} ({target}, expires in 5 minutes): {json.dumps(proposal.data, ensure_ascii=True)}", flush=True)
    confirmation = confirm(proposal)
    proposal.require_confirmation(confirmation)

    fresh, _ = collect_trace(codex_trace(paths, read_tools, f"Call {calendar_call} and tasks/list_tasks now to recheck the proposed action. Use only MCP."))
    tasks, calendar, window = read_context(fresh, calendar_range)
    payload = proposal.authorize(confirmation, tasks, calendar, setting("PLAN_B_APPROVAL_KEY", ""), window)
    print(f"Approval accepted for {proposal.id}. Executing that proposal.", flush=True)
    try:
        return verify_execution(execute_approved(paths, proposal, payload), proposal)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Write outcome unknown for {proposal.result_id}. No retry was issued. {error}")
        return False


def human_confirmation(proposal):
    if not sys.stdin.isatty():
        raise ValueError("Approval requires an interactive terminal. No write was enabled.")
    return input(f"Type approve {proposal.id} to proceed, or press Enter to decline: ")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=WRITE_TOOLS)
    parser.add_argument("--request", required=True)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--tasks", required=True, type=Path)
    parser.add_argument("--earliest", required=True, type=local_time)
    parser.add_argument("--google", action="store_true")
    parser.add_argument("--live-test", action="store_true")
    args = parser.parse_args()
    try:
        return 0 if run_action({"context": args.context, "tasks": args.tasks}, args.action, args.request, args.earliest, human_confirmation,
                               google=args.google, live_test=args.live_test) else 1
    except (ValueError, RuntimeError, EOFError, subprocess.TimeoutExpired) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
