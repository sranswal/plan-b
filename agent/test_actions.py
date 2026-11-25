"""Integration checks against Synapse-generated servers and isolated SQLite data."""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.actions import make_proposal, run_action, verify_execution
from agent.schedule import local_time
from services.action_store import signature


ROOT = Path(__file__).resolve().parents[1]
PORTS = {"context": 8000, "tasks": 8001}
EARLIEST = local_time("2026-11-23T17:00:00+05:30")


def generated_paths():
    paths = {}
    for service in PORTS:
        folder = ROOT / "services" / service
        override = os.environ.get(f"PLAN_B_TEST_{service.upper()}_SERVER")
        paths[service] = Path(override).resolve() if override else max(
            folder.glob("synapse_auto_server*.py"), key=lambda candidate: candidate.stat().st_mtime
        )
    return paths


@asynccontextmanager
async def client(name, paths):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(paths[name])],
        env={"ENDPOINT_BASE_URL": f"http://127.0.0.1:{PORTS[name]}"},
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            yield session


async def read(session, tool, **params):
    result = await session.call_tool(tool, params)
    if result.isError:
        raise AssertionError(result.content)
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def wait_for_server(port, process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError(f"Test backend failed to start on {port}. Ensure both demo ports are free.")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Test backend did not start on {port}")


def require_test_ports():
    for port in PORTS.values():
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(f"Test port {port} is unavailable. Stop the live Plan B servers before running integration tests.") from None


class ActionsTest(unittest.TestCase):
    def setUp(self):
        require_test_ports()
        self.paths = generated_paths()
        self.secret = secrets.token_hex(32)
        state = self.enterContext(tempfile.TemporaryDirectory(prefix="plan-b-actions-"))
        self.enterContext(patch.dict(os.environ, {"PLAN_B_APPROVAL_KEY": self.secret, "PLAN_B_STATE_DIR": state,
                                                 "PLAN_B_CALENDAR_PROVIDER": "fixture", "PLAN_B_LIVE_TASK_DEADLINE": ""}))
        for name, port in PORTS.items():
            process = subprocess.Popen([
                sys.executable, "-m", "uvicorn", f"services.{name}.main:app", "--port", str(port), "--log-level", "warning",
            ], cwd=ROOT)
            self.addCleanup(self.stop, process)
            wait_for_server(port, process)

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)

    def test_mcp_writes_and_safety(self):
        asyncio.run(self.check_mcp_writes_and_safety())

    async def check_mcp_writes_and_safety(self):
        async with client("context", self.paths) as context, client("tasks", self.paths) as tasks:
            self.assertEqual({tool.name for tool in (await context.list_tools()).tools}, {
                "list_commitments", "list_calendar_range", "get_calendar_availability", "create_calendar_event", "get_calendar_event", "extract_email_deadline",
            })
            self.assertEqual({tool.name for tool in (await tasks.list_tools()).tools}, {
                "list_tasks", "list_routines", "create_task", "get_task", "get_routine_preferences",
            })
            calendar = (await read(context, "list_commitments"))["commitments"]
            pending = (await read(tasks, "list_tasks"))["tasks"]
            event = make_proposal("calendar", {"task_id": "task-101"}, pending, calendar, EARLIEST)
            task = make_proposal("task", {
                "title": "Prepare walkthrough", "deadline": "2026-11-25T18:00:00+05:30",
            }, pending, calendar, EARLIEST)
            for confirmation in ("", "approve wrong-id"):
                with self.assertRaisesRegex(ValueError, "declined|did not match"):
                    event.authorize(confirmation, pending, calendar, self.secret)
            with patch("agent.actions.time.time", return_value=event.expires):
                with self.assertRaisesRegex(ValueError, "expired"):
                    event.authorize(f"approve {event.id}", pending, calendar, self.secret)

            approved_event = event.authorize(f"approve {event.id}", pending, calendar, self.secret)
            altered = {**approved_event, "title": "Changed without approval"}
            self.assertTrue((await context.call_tool("create_calendar_event", altered)).isError)
            expired = event.expires - 600
            expired_payload = {
                **approved_event, "expires": expired,
                "approval_signature": signature(self.secret, "calendar", event.id, expired, event.calendar_version, event.data),
            }
            self.assertTrue((await context.call_tool("create_calendar_event", expired_payload)).isError)
            self.assertEqual((await read(context, "list_commitments"))["commitments"], calendar)
            self.assertEqual((await read(tasks, "list_tasks"))["tasks"], pending)

            conflict = {**event.data, "starts_at": "2026-11-23T17:40:00+05:30"}
            conflicting_payload = {
                **approved_event, **conflict,
                "approval_signature": signature(self.secret, "calendar", event.id, event.expires, event.calendar_version, conflict),
            }
            self.assertTrue((await context.call_tool("create_calendar_event", conflicting_payload)).isError)
            self.assertEqual((await read(context, "list_commitments"))["commitments"], calendar)

            other_event = make_proposal("calendar", {"task_id": "task-101"}, pending, calendar, EARLIEST)
            approved_other = other_event.authorize(f"approve {other_event.id}", pending, calendar, self.secret)
            created = await read(context, "create_calendar_event", **approved_event)
            replay = await read(context, "create_calendar_event", **approved_event)
            self.assertEqual(created, replay)
            self.assertEqual(created, await read(context, "get_calendar_event", id=event.id))
            self.assertTrue((await context.call_tool("create_calendar_event", approved_other)).isError)
            updated = (await read(context, "list_commitments"))["commitments"]
            self.assertEqual(len(updated), len(calendar) + 1)
            with self.assertRaisesRegex(ValueError, "stale"):
                task.authorize(f"approve {task.id}", pending, updated, self.secret)

            task = make_proposal("task", {"title": "Prepare walkthrough", "deadline": task.data["deadline"]}, pending, updated, EARLIEST)
            approved_task = task.authorize(f"approve {task.id}", pending, updated, self.secret)
            saved = await read(tasks, "create_task", **approved_task)
            self.assertEqual(saved, await read(tasks, "create_task", **approved_task))
            self.assertEqual(saved, await read(tasks, "get_task", id=task.id))
            self.assertEqual(saved["deadline"], "2026-11-25T18:00:00+05:30")
            self.assertEqual(len((await read(tasks, "list_tasks"))["tasks"]), len(pending) + 1)

    def test_missing_readback_is_unverified(self):
        proposal = make_proposal("task", {
            "title": "Prepare walkthrough", "deadline": "2026-11-25T18:00:00+05:30",
        }, [], [], EARLIEST)
        with patch("builtins.print") as output:
            self.assertFalse(verify_execution([], proposal))
        self.assertIn("No retry was issued", output.call_args.args[0])

    def test_discovered_preferences_tool(self):
        from agent.weekly import RoutinePreference

        async def check():
            async with client("tasks", self.paths) as session:
                result = await read(session, "get_routine_preferences")
                rules = RoutinePreference.model_validate(result["routine_preferences"][0])
                self.assertEqual((rules.required_sessions_per_week, rules.preferred_session_minutes), (3, 60))
                self.assertTrue(rules.can_move)

        asyncio.run(check())

    @unittest.skipUnless(os.environ.get("PLAN_B_TEST_CODEX") == "1", "requires signed-in Codex CLI")
    def test_signed_in_read_regressions(self):
        launcher = [
            "pwsh", "-NoProfile", "-File", str(ROOT / "agent/demo.ps1"),
            "-Python", sys.executable, "-Server", str(self.paths["context"]),
            "-TasksServer", str(self.paths["tasks"]),
        ]
        for flags, expected in [([], "6:15 PM"), (["-TwoSystems"], "18:15 to 2026-11-23 19:45")]:
            with self.subTest(flags=flags):
                env = {key: value for key, value in os.environ.items() if key != "PLAN_B_APPROVAL_KEY"}
                result = subprocess.run([*launcher, *flags], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('"tool":"list_commitments"', result.stdout)
                self.assertIn(expected, result.stdout)
                if flags:
                    self.assertIn('"tool":"list_tasks"', result.stdout)

    @unittest.skipUnless(os.environ.get("PLAN_B_TEST_CODEX") == "1", "requires signed-in Codex CLI")
    def test_signed_in_discovery(self):
        result = subprocess.run([
            "pwsh", "-NoProfile", "-File", str(ROOT / "agent/demo.ps1"), "-Weekly",
            "-Python", sys.executable, "-Server", str(self.paths["context"]), "-TasksServer", str(self.paths["tasks"]),
            "-Request", "Can I fit three gym sessions into this week while protecting my meetings and hackathon work?",
        ], cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"}, text=True, encoding="utf-8", capture_output=True, timeout=150)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for call in ("context/list_commitments", "tasks/list_tasks", "tasks/get_routine_preferences"):
            self.assertIn(f"MCP {call}: completed", result.stdout)
        self.assertIn("Yes. 3 sessions fit", result.stdout)
        self.assertIn("Reserved work for Finish README", result.stdout)
        self.assertNotIn("MCP context/create_calendar_event", result.stdout)
        self.assertNotIn("MCP tasks/create_task", result.stdout)

    @unittest.skipUnless(os.environ.get("PLAN_B_TEST_CODEX") == "1", "requires signed-in Codex CLI")
    def test_signed_in_model(self):
        def approve_fixture(proposal):
            print(f"Isolated test operator approves {proposal.id}", flush=True)
            return f"approve {proposal.id}"

        self.assertTrue(run_action(self.paths, "calendar", "Find 90 minutes to finish my hackathon README and put it on my calendar.", EARLIEST, approve_fixture))
        self.assertTrue(run_action(self.paths, "task", "Add a task to prepare my hackathon demo walkthrough, due November 25 at 6 PM IST.", EARLIEST, approve_fixture))
