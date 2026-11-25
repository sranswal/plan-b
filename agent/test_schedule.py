import unittest
from datetime import timedelta

from agent.schedule import find_slot, local_time, plan_from_trace


def clock(hour, minute=0):
    return local_time(f"2026-11-23T{hour:02}:{minute:02}:00+05:30")


def completed_tool(server, tool, data):
    return {"type": "item.completed", "item": {
        "type": "mcp_tool_call", "server": server, "tool": tool, "status": "completed",
        "result": {"structured_content": data},
    }}


class ScheduleTest(unittest.TestCase):
    duration = timedelta(minutes=90)

    def test_skips_overlapping_and_nested_events(self):
        occupied = [(clock(17, 30), clock(18, 15)), (clock(17, 45), clock(18))]
        start, end = find_slot(clock(17), clock(20), self.duration, occupied)
        self.assertEqual(end - start, self.duration)
        self.assertTrue(all(end <= busy_start or start >= busy_end for busy_start, busy_end in occupied))
        self.assertLess(end, clock(20))
        self.assertEqual(start, clock(18, 15))

    def test_uses_adjacent_gap(self):
        occupied = [(clock(18, 30), clock(19))]
        self.assertEqual(find_slot(clock(17), clock(20), self.duration, occupied), (clock(17), clock(18, 30)))

    def test_rejects_finish_at_deadline(self):
        self.assertIsNone(find_slot(clock(18, 30), clock(20), self.duration, []))

    def test_converts_utc_and_rejects_naive_timestamp(self):
        self.assertEqual(local_time("2026-11-23T12:45:00Z"), clock(18, 15))
        with self.assertRaises(ValueError):
            local_time("2026-11-23T18:15:00")

    def test_trace_requires_both_servers_and_reports_infeasibility(self):
        task = {
            "id": "test", "title": "Demo submission", "status": "pending",
            "deadline": "2026-11-23T20:00:00+05:30", "estimated_minutes": 90,
        }
        trace = [
            completed_tool("tasks", "list_tasks", {"tasks": [task]}),
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"task_id":"test"}'}},
        ]
        with self.assertRaisesRegex(ValueError, "context.*list_commitments"):
            plan_from_trace(trace, clock(19))
        trace.insert(1, completed_tool("context", "list_commitments", {"commitments": []}))
        self.assertIn("No free 90-minute slot", plan_from_trace(trace, clock(19)))
