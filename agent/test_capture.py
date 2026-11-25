import unittest

from agent.web import stated_minutes
from services.context.email_deadline import extract_deadline


class CaptureTests(unittest.TestCase):
    reference = "2026-11-23T16:00:00+05:30"

    def test_email_extracts_an_explicit_deadline_with_evidence(self):
        sentence = "Please send the deck by Friday at 5 PM IST."
        self.assertEqual(extract_deadline(sentence, self.reference), {
            "deadline": "2026-11-27T17:00:00+05:30", "source_quote": sentence,
        })

    def test_email_declines_ambiguity_and_unstated_times(self):
        for text in ("Submit by Friday.", "Not due Friday at 5 PM IST.", "Submit by Friday at 5 PM PST.",
                     "Submit by Friday at 5 PM IST.\nFinish by Thursday at 6 PM IST."):
            with self.subTest(text=text):
                self.assertIsNone(extract_deadline(text, self.reference)["deadline"])

    def test_email_preserves_written_month_deadline_and_ist(self):
        sentence = "Submission deadline: November 25, 2026 at 11:59 PM IST"
        self.assertEqual(extract_deadline(sentence, self.reference), {
            "deadline": "2026-11-25T23:59:00+05:30", "source_quote": sentence,
        })
        self.assertIsNone(extract_deadline("Deadline: November 31, 2026 at 11:59 PM IST", self.reference)["deadline"])

    def test_activity_duration_must_be_present_in_the_request(self):
        self.assertEqual(stated_minutes("Find a 15-minute check-in"), {15})
        self.assertEqual(stated_minutes("a forty-five minute walk"), {45})
        self.assertEqual(stated_minutes("Find time for a walk"), set())
