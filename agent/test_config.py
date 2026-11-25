import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.config import setting
from services.context import google_oauth


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("services.config.PROJECT_ROOT", self.root))
        self.enterContext(patch("services.context.google_oauth.PROJECT_ROOT", self.root))
        self.enterContext(patch.dict(os.environ, clear=True))
        (self.root / ".env").write_text(
            "PLAN_B_CALENDAR_PROVIDER=google\nPLAN_B_GOOGLE_CLIENT_FILE='../private/client.json'\n"
            "PLAN_B_GOOGLE_CALENDAR_ID=test@example.invalid\nPLAN_B_LIVE_TASK_MINUTES=15\n",
            encoding="utf-8-sig",
        )

    def test_shell_overrides_project_configuration_without_exporting_it(self):
        self.assertEqual(setting("PLAN_B_CALENDAR_PROVIDER"), "google")
        self.assertEqual(setting("PLAN_B_LIVE_TASK_MINUTES"), "15")
        self.assertNotIn("PLAN_B_GOOGLE_CALENDAR_ID", os.environ)
        with patch.dict(os.environ, {"PLAN_B_CALENDAR_PROVIDER": "fixture"}):
            self.assertEqual(setting("PLAN_B_CALENDAR_PROVIDER"), "fixture")

    def test_connect_accepts_no_flags_and_explicit_flags_win(self):
        with patch.object(google_oauth, "Vault"), patch.object(google_oauth, "connect") as connect:
            for flags, client, calendar in (
                ([], self.root / "../private/client.json", "test@example.invalid"),
                (["--client", "local.json", "--calendar-id", "override@example.invalid"], Path("local.json"), "override@example.invalid"),
            ):
                with patch.object(sys, "argv", ["google_oauth", "connect", *flags]):
                    google_oauth.main()
                args = connect.call_args.args[0]
                self.assertEqual(Path(args.client), client)
                self.assertEqual(args.calendar_id, calendar)
                self.assertEqual(args.busy_calendar, [])
