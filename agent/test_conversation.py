import json
import os
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from threading import Lock
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from agent.intent import Intent, gemini_intent
from agent.web import Demo, PlannerRequest, activity_slot, app, stated_minutes
from services.action_store import signature, version
from services.context.google_oauth import GoogleError
from services.tasks.google_tasks import GoogleTasks, normalized


class FakeTasks:
    def __init__(self):
        self.entries = {}
        self.posts = 0
        self.fail = False

    def profile(self):
        return {"connection_id": "profile", "list_id": "selected-list"}

    def request(self, profile, method, path, **kwargs):
        if method == "POST":
            self.posts += 1
            if self.fail:
                raise GoogleError(503, "Uncertain write")
            entry = {"id": "google-123", **kwargs["json"]}
            self.entries[entry["id"]] = entry
            return entry
        if path.endswith("/tasks"):
            return {"items": list(self.entries.values())}
        return self.entries[path.rsplit("/", 1)[1]]


class GoogleTasksTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.key = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {"PLAN_B_STATE_DIR": folder, "PLAN_B_APPROVAL_KEY": self.key}))
        self.connection = FakeTasks()
        self.tasks = GoogleTasks(self.connection)
        self.data = {"title": "Send the deck", "deadline": "2026-11-27T17:00:00+05:30", "status": "pending",
                     "estimated_minutes": None, "priority": "medium"}
        expires = int(time.time()) + 200
        snapshot = version([])
        self.approval = {"proposal_id": "proposal", "expires": expires, "snapshot": snapshot,
                         "approval_signature": signature(self.key, "task", "proposal", expires, snapshot, self.data)}

    def test_real_provider_contract_requests_selected_list_and_verifies_saved_task(self):
        written = self.tasks.create(self.approval, self.data)
        self.assertEqual(self.tasks.get("proposal"), written)
        self.assertEqual(self.tasks.list(), [written])
        self.assertEqual(written["provider_id"], "google-123")
        self.assertEqual(self.connection.entries["google-123"]["due"], "2026-11-27T00:00:00Z")
        self.assertIn("2026-11-27T17:00:00+05:30", self.connection.entries["google-123"]["notes"])
        self.assertEqual(self.tasks.create(self.approval, self.data), written)
        self.assertEqual(self.connection.posts, 1)

    def test_uncertain_write_is_not_retried(self):
        self.connection.fail = True
        with self.assertRaises(GoogleError):
            self.tasks.create(self.approval, self.data)
        with self.assertRaises(HTTPException) as error:
            self.tasks.create(self.approval, self.data)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.connection.posts, 1)

    def test_external_due_date_has_no_fabricated_clock_time(self):
        record = normalized({"id": "external", "title": "Original", "due": "2026-11-27T00:00:00Z"})
        self.assertEqual(record["deadline"], "2026-11-27")
        self.assertIsNone(record["estimated_minutes"])


class ConversationTests(unittest.TestCase):
    def test_fixture_focus_proposal_matches_unmodified_task_snapshot(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"PLAN_B_WEB_GOOGLE": "0", "PLAN_B_WEB_GOOGLE_TASKS": "0"}
        ):
            demo = Demo(folder)
            tasks = [{"id": "readme", "title": "Finish README", "status": "pending",
                      "deadline": "2026-11-23T20:00:00+05:30", "estimated_minutes": 90}]
            results = {("tasks", "list_tasks"): {"tasks": tasks},
                       ("context", "list_commitments"): {"commitments": []}}
            with patch.object(demo, "trace", return_value=[]), patch(
                "agent.web.collect_trace", return_value=(results, '{"task_id":"readme"}')
            ), patch("agent.web.plan_from_trace", return_value="Suggested slot"):
                result = demo.plan(PlannerRequest(mode="focus", question="Find time for my README"))
            proposal = demo.pending[result["proposal"]["id"]][0]
            self.assertEqual(proposal.authorize(f"approve {proposal.id}", tasks, [], demo.key)["snapshot"], version([]))

    def test_gmail_preview_requires_paired_extension_and_defers_approval(self):
        from agent.web import decide_proposal

        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        due = (now + timedelta(days=1)).replace(microsecond=0)
        window = {"window_start": now.isoformat(), "window_end": (now + timedelta(days=7)).isoformat(), "revision": "snapshot"}
        demo = SimpleNamespace(google=True, pending={}, lock=Lock(), calendar_proposal=Demo.calendar_proposal,
                               call=AsyncMock(side_effect=[
                                   {"deadline": due.isoformat(), "source_quote": "Submit by the deadline."},
                                   {"commitments": [], "google_window": window},
                                   {"tasks": []},
                               ]))
        extension_id = "a" * 32
        origin = {"Origin": f"chrome-extension://{extension_id}"}
        body = {"source": "https://mail.google.com/mail/u/0/#inbox/synthetic", "subject": "Demo",
                "message": "Deadline: 2080-01-01 at 18:00 IST", "timezone": "Asia/Kolkata"}

        def consume(demo, token, decision):
            self.assertEqual(decision, "approve")
            return {"status": "verified"} if demo.pending.pop(token, None) else decide_proposal(demo, token, decision)

        with patch.dict(os.environ, {"PLAN_B_GMAIL_EXTENSION_ID": extension_id}), patch.object(
            app.state, "demo", demo, create=True
        ), patch("agent.web.decide_proposal", Mock(side_effect=consume)) as execute:
            client = TestClient(app, base_url="http://127.0.0.1")
            self.assertEqual(client.post("/api/gmail/preview", json=body).status_code, 403)
            preview = client.post("/api/gmail/preview", headers=origin, json=body)
            self.assertEqual(preview.status_code, 200)
            token = preview.json()["token"]
            self.assertEqual(demo.pending[token][0].data["transparency"], "transparent")
            execute.assert_not_called()
            route = f"/api/gmail/{token}/approve"
            self.assertEqual(client.post(route).status_code, 403)
            self.assertEqual(client.post(route, headers=origin).json(), {"status": "verified"})
            self.assertEqual(client.post(route, headers=origin).status_code, 409)

    def test_movie_duration_reply_combines_hours_and_minutes(self):
        demo = object.__new__(Demo)
        demo.google = False
        original = "Can I go for a movie this Sunday?"
        clarify = Intent(mode="clarify", book=False, title="", deadline="", start="", end="",
                         exact_start=False, clarification="How long?")
        with patch.object(demo, "classify", return_value=(clarify, "Gemini")) as classify:
            demo.plan(PlannerRequest(question="Probably 3 hours, 30 minutes.", follow_up_to=original))
            self.assertEqual(classify.call_args.args[0], original + "\nClarification reply: Probably 3 hours, 30 minutes.")
        self.assertEqual(stated_minutes("Find three hours and thirty minutes for a movie"), {210})
        self.assertEqual(stated_minutes("30 minutes or 1 hour"), {30, 60})
        self.assertEqual(stated_minutes("3 hours 45 minutes would be fine"), {225})

    def test_time_revision_keeps_booking_request_and_duration(self):
        demo = object.__new__(Demo)
        demo.google = False
        clarify = Intent(mode="clarify", book=False, title="", deadline="", start="", end="",
                         exact_start=False, clarification="Which day?")
        with patch.object(demo, "classify", return_value=(clarify, "Gemini")) as classify:
            demo.plan(PlannerRequest(question="Sunday evening", revise_time=True,
                                     follow_up_to="Book 60 minutes for the gym Saturday morning"))
            self.assertEqual(classify.call_args.args[0], "Book 60 minutes for the gym Saturday morning\nUpdated time preference: Sunday evening")

    def test_activity_defaults_to_daytime_but_honors_explicit_overnight_time(self):
        zone = ZoneInfo("Asia/Kolkata")
        midnight = datetime(2026, 11, 28, tzinfo=zone)
        duration = timedelta(hours=1)
        busy = [(midnight.replace(hour=7), midnight.replace(hour=8))]
        slot = activity_slot(midnight, midnight + timedelta(days=2), duration, busy, zone)
        self.assertEqual(slot[0], midnight.replace(hour=8))
        slot = activity_slot(midnight, midnight + duration, duration, busy, zone, explicit_clock_time=True)
        self.assertEqual(slot[0], midnight)

    def test_duration_reply_keeps_original_activity_and_weekend(self):
        demo = object.__new__(Demo)
        demo.google = False
        original = "Plan a gym session for this weekend"
        clarify = Intent(mode="clarify", book=False, title="", deadline="", start="", end="",
                         exact_start=False, clarification="How much time?")
        with patch.object(demo, "classify", return_value=(clarify, "Gemini")) as classify:
            first = demo.plan(PlannerRequest(question=original))
            demo.plan(PlannerRequest(question="1 hour max", follow_up_to=first["follow_up"]))
            self.assertEqual(classify.call_args.args[0], original + "\nClarification reply: 1 hour max")
            demo.plan(PlannerRequest(question="30 minutes in the week, in the evening", follow_up_to=original))
            self.assertEqual(classify.call_args.args[0], original + "\nClarification reply: 30 minutes in the week, in the evening")
            for reply in ("30 MINTE", "30 MIN"):
                demo.plan(PlannerRequest(question=reply, follow_up_to=original))
                self.assertEqual(classify.call_args.args[0], original + "\nClarification reply: " + reply.replace("MINTE", "minute"))
            demo.plan(PlannerRequest(question="Show my calendar"))
            self.assertEqual(classify.call_args.args[0], "Show my calendar")

    def test_effort_override_is_persisted_and_scoped_without_changing_source_tasks(self):
        demo = object.__new__(Demo)
        tasks = [{"id": "task-1", "estimated_minutes": 30}]
        with tempfile.TemporaryDirectory() as folder:
            demo.effort_db = Path(folder) / "effort.sqlite3"
            demo.effort_scope = "first-list"
            demo.task_effort(tasks, ("task-1", 75))
            self.assertEqual(demo.task_effort(tasks)[0]["estimated_minutes"], 75)
            self.assertEqual(tasks[0]["estimated_minutes"], 30)
            demo.effort_scope = "second-list"
            self.assertEqual(demo.task_effort(tasks)[0]["estimated_minutes"], 30)
            demo.effort_scope = "first-list"
            self.assertEqual(demo.task_effort(tasks, ("task-1", None))[0]["estimated_minutes"], 30)

    def test_single_activity_does_not_expand_into_weekly_routine(self):
        demo = object.__new__(Demo)
        demo.google = False
        intent = Intent(mode="routine", book=True, title="Gym", deadline="", start="", end="",
                        exact_start=False, clarification="")
        with patch.object(demo, "classify", return_value=(intent, "Gemini")), patch.object(demo, "trace") as trace:
            response = demo.plan(PlannerRequest(question="I need to go to the gym"))
        trace.assert_not_called()
        self.assertIsNone(response["proposal"])
        self.assertIn("one session", response["answer"])

    def test_routine_example_keeps_an_explicit_count_of_gym_sessions(self):
        demo = object.__new__(Demo)
        demo.google = False
        intent = Intent(mode="routine", book=False, title="Gym", deadline="", start="", end="",
                        exact_start=False, clarification="")
        with patch.object(demo, "classify", return_value=(intent, "Gemini")), patch.object(
            demo, "catalogs", return_value={"tasks": []}
        ), patch.object(demo, "trace", side_effect=RuntimeError("routine tool requested")) as trace:
            with self.assertRaisesRegex(RuntimeError, "routine tool requested"):
                demo.plan(PlannerRequest(question="Can I fit three gym sessions into this week?"))
        self.assertEqual(trace.call_args.args[2], "routine-selection.schema.json")

    def test_gemini_validates_request_and_uses_key_header(self):
        output = {"mode": "activity", "book": False, "title": "Walk", "deadline": "", "start": "", "end": "",
                  "exact_start": False, "clarification": ""}
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch("agent.intent.requests.post") as post:
            post.return_value.ok = True
            post.return_value.json.return_value = {"candidates": [{"content": {"parts": [{"text": json.dumps(output)}]}}]}
            self.assertFalse(gemini_intent("Find a walk slot").book)
            self.assertEqual(post.call_args.kwargs["headers"], {"x-goog-api-key": "test-key"})

    def test_ambiguous_follow_up_creates_no_proposal(self):
        demo = object.__new__(Demo)
        demo.last_candidate = None
        response = demo.plan(PlannerRequest(question="Book it"))
        self.assertIsNone(response["proposal"])
