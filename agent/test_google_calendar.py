import asyncio
import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from requests import Response, Timeout

from agent.actions import make_proposal, run_action
from agent.schedule import local_time
from agent import test_actions as integration
from services.context import main as context
from services.context.calendar_providers import GoogleCalendarProvider
from services.context.google_oauth import GoogleConnection, GoogleError, RECONNECT, SCOPE, google_request


START = "2099-01-05T18:00:00+05:30"
END = "2099-01-12T18:00:00+05:30"
DEADLINE = "2099-01-05T22:00:00+05:30"
COMMITMENTS = f"/calendar/commitments/{START}/{END}"


class MockGoogle:
    def __init__(self):
        self.data = {"connection_id": "test-profile", "calendar": "test@group.calendar.google.com",
                     "calendars": ["test@group.calendar.google.com", "primary"]}
        self.saved = {}
        self.calls = []
        self.fail_after_insert = False
        self.extra_busy = False

    def profile(self):
        return self.data

    def request(self, profile, method, path, **kwargs):
        params = kwargs.get("params", {}).copy()
        self.calls.append((method, path, params))
        if method == "POST":
            record = kwargs["json"]
            assert "attendees" not in record
            self.saved[record["id"]] = record
            if self.fail_after_insert:
                raise GoogleError(503, "Mock lost acknowledgment")
            return record
        if not path.endswith("/events"):
            event_id = path.rsplit("/", 1)[1]
            if event_id not in self.saved:
                raise GoogleError(404, "No event")
            return self.saved[event_id]
        if "/primary/" in path:
            if params.get("pageToken") == "page-2":
                return {"timeZone": "America/Los_Angeles", "items": [
                    {"id": "all-day", "start": {"date": "2099-01-06"}, "end": {"date": "2099-01-07"}},
                ]}
            events = [
                {"id": "instance", "summary": "PRIVATE TITLE", "recurringEventId": "series",
                 "start": {"dateTime": "2099-01-05T12:45:00Z"}, "end": {"dateTime": "2099-01-05T13:15:00Z"}},
                {"id": "free", "transparency": "transparent"},
                {"id": "cancelled", "status": "cancelled"},
            ]
            if self.extra_busy:
                events.append({"id": "new", "start": {"dateTime": "2099-01-05T20:00:00+05:30"},
                               "end": {"dateTime": "2099-01-05T22:00:00+05:30"}})
            return {"timeZone": "America/Los_Angeles", "items": events, "nextPageToken": "page-2"}
        return {"timeZone": "Asia/Kolkata", "items": list(self.saved.values())}


def mock_app():
    context.google = GoogleCalendarProvider(MockGoogle())
    return context.app


class GoogleCalendarTest(unittest.TestCase):
    def setUp(self):
        self.google = MockGoogle()
        self.secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {"PLAN_B_CALENDAR_PROVIDER": "google", "PLAN_B_APPROVAL_KEY": self.secret}))
        self.enterContext(patch.object(context, "google", GoogleCalendarProvider(self.google)))
        self.api = TestClient(context.app)

    def propose(self):
        view = self.api.get(COMMITMENTS).json()
        tasks = [{"id": "test", "title": "Test focus", "deadline": DEADLINE, "estimated_minutes": 90, "status": "pending"}]
        proposal = make_proposal("calendar", {"task_id": "test"}, tasks, view["commitments"], local_time(START), view["google_window"])
        return proposal, view, tasks

    def approve(self, proposal, view, tasks):
        return proposal.authorize(f"approve {proposal.id}", tasks, view["commitments"], self.secret, view["google_window"])

    def test_pages_recurring_all_day_timezone_and_privacy(self):
        response = self.api.get(COMMITMENTS)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("PRIVATE TITLE", response.text)
        events = response.json()["commitments"]
        self.assertEqual([e["id"] for e in events], ["instance", "all-day"])
        self.assertEqual(events[0]["starts_at"], "2099-01-05T18:15:00+05:30")
        self.assertEqual(events[1]["starts_at"], "2099-01-06T13:30:00+05:30")
        self.assertEqual(events[1]["ends_at"], "2099-01-07T13:30:00+05:30")
        self.assertTrue(any(params.get("pageToken") == "page-2" for _, _, params in self.google.calls))
        self.assertTrue(all(params["singleEvents"] == "true" for _, _, params in self.google.calls))
        availability = self.api.get(f"/calendar/availability/{START}/{END}")
        self.assertEqual(availability.status_code, 200)
        self.assertEqual(len(availability.json()["occupied"]), 2)
        self.assertEqual(self.api.get("/commitments").status_code, 422)

    def test_declined_approval_stale_conflict_and_successful_readback(self):
        proposal, view, tasks = self.propose()
        with self.assertRaisesRegex(ValueError, "declined"):
            proposal.authorize("", tasks, view["commitments"], self.secret, view["google_window"])
        payload = self.approve(proposal, view, tasks)
        self.assertEqual(self.api.post("/calendar/events", json={**payload, "title": "Changed"}).status_code, 403)
        self.google.extra_busy = True
        self.assertEqual(self.api.post("/calendar/events", json=payload).status_code, 409)
        self.assertFalse(self.google.saved)
        self.google.extra_busy = False
        response = self.api.post("/calendar/events", json=payload)
        self.assertEqual(response.status_code, 200)
        saved = response.json()
        self.assertEqual(saved["starts_at"], "2099-01-05T18:45:00+05:30")
        self.assertEqual(saved, self.api.get(f"/calendar/events/{proposal.result_id}").json())
        self.assertEqual(saved, self.api.post("/calendar/events", json=payload).json())
        posts = [path for method, path, _ in self.google.calls if method == "POST"]
        self.assertEqual(posts, ["/calendars/test%40group.calendar.google.com/events"])

    def test_lost_acknowledgment_leaves_readable_id_without_reposting(self):
        proposal, view, tasks = self.propose()
        self.google.fail_after_insert = True
        self.assertEqual(self.api.post("/calendar/events", json=self.approve(proposal, view, tasks)).status_code, 503)
        self.assertEqual(self.api.get(f"/calendar/events/{proposal.result_id}").status_code, 200)
        self.assertEqual(sum(method == "POST" for method, *_ in self.google.calls), 1)

    def test_calendar_conflict_rejected_even_with_matching_revision(self):
        from dataclasses import replace

        proposal, view, tasks = self.propose()
        proposal = replace(proposal, data={**proposal.data, "starts_at": START})
        response = self.api.post("/calendar/events", json=self.approve(proposal, view, tasks))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "Calendar conflict")
        self.assertFalse(self.google.saved)

    def test_deadline_marker_does_not_block_availability(self):
        from dataclasses import replace

        proposal, view, tasks = self.propose()
        proposal = replace(proposal, data={**proposal.data, "starts_at": "2099-01-05T18:20:00+05:30",
                                           "ends_at": "2099-01-05T18:21:00+05:30", "transparency": "transparent"})
        result = self.api.post("/calendar/events", json=self.approve(proposal, view, tasks))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), self.api.get(f"/calendar/events/{proposal.result_id}").json())
        self.assertEqual(self.api.get(COMMITMENTS).json()["commitments"], view["commitments"])

    def test_disconnected_response_is_friendly(self):
        with patch.object(self.google, "profile", side_effect=GoogleError(401, RECONNECT)):
            response = self.api.get(COMMITMENTS)
        self.assertEqual(response.status_code, 401)
        self.assertIn("connect command", response.json()["detail"])


def mock_response(status, payload):
    response = Response()
    response.status_code = status
    response._content = json.dumps(payload).encode()
    return response


class OAuthTest(unittest.TestCase):
    def test_expired_access_token_refreshes_and_is_saved_to_vault(self):
        profile = {"connection_id": "test", "credentials": {
            "token": "old-mock", "refresh_token": "mock-refresh", "client_id": "mock-client",
            "client_secret": "mock-secret", "expiry": "2000-01-01T00:00:00Z", "scopes": [SCOPE],
        }}

        def transport(_, request, **kwargs):
            if request.url == "https://oauth2.googleapis.com/token":
                return mock_response(200, {"access_token": "new-mock", "expires_in": 3600, "token_type": "Bearer"})
            self.assertEqual(request.headers["Authorization"], "Bearer new-mock")
            return mock_response(200, {"items": []})

        with patch("services.context.google_oauth.Vault") as vault, patch("requests.adapters.HTTPAdapter.send", transport):
            vault.return_value.load.return_value = profile
            self.assertEqual(GoogleConnection().request(profile, "GET", "/calendars/primary/events"), {"items": []})
        self.assertEqual(vault.return_value.save.call_args.args[0]["credentials"]["token"], "new-mock")

    def test_revoked_refresh_grant_requires_reconnect(self):
        from google.oauth2.credentials import Credentials

        credentials = Credentials.from_authorized_user_info({
            "refresh_token": "mock-refresh", "client_id": "mock-client", "client_secret": "mock-secret",
        }, scopes=[SCOPE])
        with patch("requests.adapters.HTTPAdapter.send", return_value=mock_response(400, {"error": "invalid_grant"})) as send:
            with self.assertRaisesRegex(GoogleError, "connect command"):
                google_request(credentials, "GET", "/calendars/primary/events")
        self.assertEqual(send.call_count, 1)

    def test_transport_errors_are_sanitized_and_rejected_post_is_not_retried(self):
        from google.oauth2.credentials import Credentials

        for status in (401, 403, 429, 500):
            with self.subTest(status=status), patch("requests.adapters.HTTPAdapter.send", return_value=mock_response(status, {"private": "DO NOT LOG"})) as send:
                with self.assertRaises(GoogleError) as caught:
                    google_request(Credentials(token="mock"), "POST", "/calendars/primary/events")
                self.assertNotIn("DO NOT LOG", str(caught.exception))
                self.assertEqual(caught.exception.status, status if status < 429 else 503)
                self.assertEqual(send.call_count, 1)
        with patch("requests.adapters.HTTPAdapter.send", side_effect=Timeout("DO NOT LOG")):
            with self.assertRaisesRegex(GoogleError, "Could not reach"):
                google_request(Credentials(token="mock"), "GET", "/calendars/primary/events")


class GoogleMCPTest(unittest.TestCase):
    def setUp(self):
        integration.require_test_ports()
        self.paths = integration.generated_paths()
        state = self.enterContext(tempfile.TemporaryDirectory(prefix="plan-b-google-mock-"))
        self.enterContext(patch.dict(os.environ, {"PLAN_B_CALENDAR_PROVIDER": "google", "PLAN_B_APPROVAL_KEY": secrets.token_hex(32),
                                                 "PLAN_B_STATE_DIR": state, "PLAN_B_LIVE_TASK_DEADLINE": DEADLINE,
                                                 "PLAN_B_LIVE_TASK_MINUTES": "90"}))
        for port, app in ((8000, "agent.test_google_calendar:mock_app"), (8001, "services.tasks.main:app")):
            cmd = [sys.executable, "-m", "uvicorn", app, "--port", str(port), "--no-access-log", "--log-level", "warning"]
            if port == 8000:
                cmd.append("--factory")
            process = subprocess.Popen(cmd, cwd=integration.ROOT)
            self.addCleanup(integration.ActionsTest.stop, process)
            integration.wait_for_server(port, process)

    def test_generated_mcp_range_and_availability(self):
        async def check():
            async with integration.client("context", self.paths) as session:
                view = await integration.read(session, "list_calendar_range", start=START, end=END)
                self.assertEqual(view["google_window"]["window_start"], START)
                self.assertEqual(len(view["commitments"]), 2)
                occupied = await integration.read(session, "get_calendar_availability", start=START, end=END)
                self.assertEqual(len(occupied["occupied"]), 2)

        asyncio.run(check())

    @unittest.skipUnless(os.environ.get("PLAN_B_TEST_CODEX") == "1", "requires signed-in Codex CLI")
    def test_signed_in_model_with_mock_google(self):
        self.assertTrue(run_action(self.paths, "calendar", "Find a slot for the labeled Plan B test task and put it on my calendar.",
                                   local_time(START), lambda proposal: f"approve {proposal.id}", google=True, live_test=True))
