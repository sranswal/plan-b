"""Extract a deadline only when a message states an unambiguous date and time."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december")
MONTH_DATE = r"(?:" + "|".join(MONTHS) + r")[ \t]+\d{1,2},?[ \t]+\d{4}"
ABSOLUTE_DATE = r"\b(?:\d{4}-\d{2}-\d{2}|" + MONTH_DATE + r")\b"
DATE = r"(?P<day>\d{4}-\d{2}-\d{2}|" + MONTH_DATE + r"|today|tomorrow|" + "|".join(WEEKDAYS) + r")"
TIME = r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?[ \t]*(?P<meridiem>am|pm)?[ \t]*(?P<zone>IST|UTC|Z|[+-]\d{2}:\d{2})?\b"
DEADLINE = re.compile(r"\b(?:by|due(?:\s+on)?|deadline(?:\s+is)?\s*:?)\s+" + DATE + r"(?:\s+at\s+|[ T])" + TIME, re.I)


def extract_deadline(message, reference, timezone="Asia/Kolkata"):
    local = ZoneInfo(timezone)
    received = datetime.fromisoformat(reference)
    if received.utcoffset() is None:
        raise ValueError("Reference timestamp needs a timezone.")
    received = received.astimezone(local)
    found = {}
    for match in DEADLINE.finditer(message):
        values = match.groupdict()
        # Reject unsupported timezone suffixes and quoted lines rather than guessing.
        tail = message[match.end():].splitlines()[0] if message[match.end():].splitlines() else ""
        line = message[message.rfind("\n", 0, match.start()) + 1:match.start()]
        if line.lstrip().startswith(">") or re.match(r"\s*(?:[A-Z]{2,5}\b|[+-]\d)", tail):
            continue
        day = values["day"].lower()
        hour, minute = int(values["hour"]), int(values["minute"] or 0)
        ampm = values["meridiem"]
        if (not ampm and values["minute"] is None) or (ampm and not 1 <= hour <= 12):
            continue
        if ampm:
            hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
        try:
            if day in WEEKDAYS:
                date = received.date() + timedelta(days=(WEEKDAYS.index(day) - received.weekday()) % 7)
            elif day in {"today", "tomorrow"}:
                date = received.date() + timedelta(days=day == "tomorrow")
            elif day[0].isalpha():
                month, number, year = day.replace(",", "").split()
                date = datetime(int(year), MONTHS.index(month) + 1, int(number)).date()
            else:
                date = datetime.fromisoformat(day).date()
            iso = f"{date}T{hour:02}:{minute:02}:00"
            if values["zone"]:
                zone = values["zone"].upper()
                offset = {"IST": "+05:30", "UTC": "+00:00", "Z": "+00:00"}.get(zone, zone)
                deadline = datetime.fromisoformat(iso + offset).astimezone(local)
            else:
                deadline = datetime.fromisoformat(iso).replace(tzinfo=local)
        except ValueError:
            continue
        if deadline < received:
            continue
        start = message.rfind("\n", 0, match.start()) + 1
        end = message.find("\n", match.end())
        quote = message[start:end if end >= 0 else len(message)].strip()
        uncertain = re.search(r"\b(?:not due|no longer|tentative|if approved|previous deadline|old deadline|subject to change)\b", quote, re.I)
        if not uncertain and len(quote) <= 1000:
            found[deadline.isoformat()] = quote
    if len(found) != 1:
        return {"deadline": None, "reason": "Provide one explicit deadline with a date and time. Multiple or unsupported deadlines require review."}
    deadline, quote = next(iter(found.items()))
    return {"deadline": deadline, "source_quote": quote}
