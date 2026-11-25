"""Find a slot using only the model's task selection and recorded MCP results."""

import argparse
import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def local_time(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() is None:
        raise ValueError(f"Timestamp must include an offset: {value}")
    return timestamp.astimezone(ZoneInfo("Asia/Kolkata"))


def task_deadline(task):
    due = task["deadline"]
    return local_time(due + "T00:00:00+05:30") + timedelta(days=1) if len(due) == 10 else local_time(due)


def find_slot(earliest, deadline, duration, events, *, allow_end=False):
    if duration <= timedelta(0):
        raise ValueError("Duration must be positive")
    candidate = earliest
    for start, end in sorted(events):
        if end <= start:
            raise ValueError("Calendar event must end after it starts")
        if candidate + duration <= start:
            break
        candidate = max(candidate, end)
    finish = candidate + duration
    if finish < deadline or (allow_end and finish == deadline):
        return candidate, finish
    return None


def collect_trace(events):
    tool_results = {}
    selection = None
    for event in events:
        if event["type"] != "item.completed":
            continue
        item = event["item"]
        if item["type"] == "mcp_tool_call" and item["status"] == "completed" and not item.get("error"):
            result = item["result"]
            structured = result.get("structured_content")
            tool_results[item["server"], item["tool"]] = structured if structured is not None else json.loads(
                next(part["text"] for part in result["content"] if part["type"] == "text")
            )
        if item["type"] == "agent_message":
            selection = item["text"]

    return tool_results, selection


def schedule_inputs(tool_results):
    required = {("tasks", "list_tasks"), ("context", "list_commitments")}
    if missing := required - tool_results.keys():
        raise ValueError(f"Agent did not complete both required MCP calls. Missing: {sorted(missing)}")
    return tool_results["tasks", "list_tasks"]["tasks"], tool_results["context", "list_commitments"]["commitments"]


def task_slot(tasks, commitments, task_id, earliest):
    task = next((task for task in tasks if task["id"] == task_id and task["status"] == "pending"), None)
    if task is None:
        raise ValueError("Agent did not select a pending task from the tool result")
    if not task.get("deadline"):
        raise ValueError("That task has no due date. Ask for a block of a specific duration instead.")

    occupied = [(local_time(event["starts_at"]), local_time(event["ends_at"])) for event in commitments]
    deadline = task_deadline(task)
    minutes = task["estimated_minutes"]
    if not isinstance(minutes, int) or minutes <= 0:
        raise ValueError("The selected task needs a positive effort estimate")
    return task, deadline, find_slot(earliest, deadline, timedelta(minutes=minutes), occupied)


def plan_from_trace(events, earliest):
    tool_results, selection = collect_trace(events)
    tasks, commitments = schedule_inputs(tool_results)
    task, deadline, slot = task_slot(tasks, commitments, json.loads(selection or "{}").get("task_id"), earliest)
    minutes = task["estimated_minutes"]
    if slot is None:
        return f'No free {minutes}-minute slot for "{task["title"]}" from {earliest:%Y-%m-%d %H:%M} before {deadline:%Y-%m-%d %H:%M} IST (Asia/Kolkata).'

    start, finish = slot
    return (
        f'Suggested slot for "{task["title"]}": {start:%Y-%m-%d %H:%M} to {finish:%Y-%m-%d %H:%M} IST (Asia/Kolkata). '
        f'It provides {minutes} uninterrupted minutes, overlaps no calendar events, and ends before the {deadline:%Y-%m-%d %H:%M} IST deadline.'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--earliest", required=True, help="ISO 8601 planning start with UTC offset")
    args = parser.parse_args()
    events = (json.loads(line) for line in sys.stdin if line.strip())
    print(plan_from_trace(events, local_time(args.earliest)))


if __name__ == "__main__":
    main()
