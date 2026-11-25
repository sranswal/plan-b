import unittest
from datetime import timedelta

from fastapi.testclient import TestClient
from mcp.types import Tool

from agent.schedule import local_time
from agent.test_schedule import completed_tool
from agent.weekly import DAYS, RoutinePreference, plan_week, read_tools, weekly_from_trace
from services.context.main import store as calendar_store
from services.tasks.main import app, store as task_store


EARLIEST = local_time("2026-11-23T17:00:00+05:30")


class WeeklyTest(unittest.TestCase):
    def setUp(self):
        response = TestClient(app).get("/routine-preferences")
        self.assertEqual(response.status_code, 200)
        self.payload = response.json()
        self.rules = RoutinePreference.model_validate(self.payload["routine_preferences"][0])

    def test_protects_calendar_and_deadline_and_obeys_preferences(self):
        calendar, tasks = calendar_store.fixtures, task_store.fixtures
        work, sessions = plan_week(tasks, calendar, self.rules, EARLIEST)
        deadlines = {task["title"]: local_time(task["deadline"]) for task in tasks}
        self.assertEqual(len(work), len(tasks))
        self.assertEqual(len(sessions), 3)
        for title, _, end in work:
            self.assertLess(end, deadlines[title])
        occupied = [(local_time(event["starts_at"]), local_time(event["ends_at"])) for event in calendar]
        occupied.extend((start, end) for _, start, end in work)
        for start, end in sessions:
            self.assertEqual(end - start, timedelta(minutes=60))
            self.assertTrue(all(end <= first or start >= last for first, last in occupied))
            day = DAYS[start.weekday()]
            self.assertIn(day, self.rules.allowed_days)
            self.assertTrue(any(day in window.days and window.start <= start.time() < end.time() <= window.end for window in self.rules.preferred_windows))
        for (_, prior_end), (start, _) in zip(sessions, sessions[1:]):
            self.assertGreaterEqual(start - prior_end, timedelta(minutes=1440))

    def test_trace_needs_structured_rules_and_honestly_reports_infeasibility(self):
        trace = [
            completed_tool("tasks", "list_tasks", {"tasks": task_store.fixtures}),
            completed_tool("context", "list_commitments", {"commitments": calendar_store.fixtures}),
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"routine_id":"routine-1"}'}},
        ]
        self.assertIn("can't validate", weekly_from_trace(trace, EARLIEST))
        rules = self.rules.model_copy(update={"minimum_rest_minutes": 10080})
        trace.append(completed_tool("tasks", "any_discovered_tool_name", {"routine_preferences": [rules.model_dump()]}))
        self.assertIn("only fit 1 of 3", weekly_from_trace(trace, EARLIEST))

    def test_catalog_discovers_read_tools_without_enabling_writes(self):
        names = read_tools([
            Tool(name="get_new_capability", inputSchema={"type": "object", "properties": {}}),
            Tool(name="get_single_record", inputSchema={"type": "object", "properties": {"id": {}}}),
            Tool(name="create_event", inputSchema={"type": "object"}),
        ])
        self.assertEqual(names, ["get_new_capability"])
