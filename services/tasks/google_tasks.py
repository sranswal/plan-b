"""Selected Google Tasks list, with a durable one-attempt journal for writes."""

import argparse
import json
import logging
import time
import uuid
from contextlib import closing
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from services.action_store import Store, aware, canonical, verify_approval, version
from services.config import PROJECT_ROOT, setting
from services.context.google_oauth import GoogleConnection, GoogleError, Vault, desktop_credentials, google_request


class TasksConnection(GoogleConnection):
    scope = "https://www.googleapis.com/auth/tasks"
    api = "https://tasks.googleapis.com/tasks/v1"
    reconnect = "Connect Google Tasks with python -m services.tasks.google_tasks connect."
    service = "Google Tasks"

    def vault(self):
        return Vault("Plan B Google Tasks")

    def profile(self):
        profile = super().profile()
        pinned = setting("PLAN_B_GOOGLE_TASKS_CONNECTION_ID")
        if pinned and profile["connection_id"] != pinned:
            raise GoogleError(409, "Google Tasks selection changed. Restart the browser coordinator.")
        return profile


NOTE_PREFIX = "\n\nPlan B tracking: "


def task_path(profile, task_id=None):
    base = f"/lists/{quote(profile['list_id'], safe='')}/tasks"
    return base if task_id is None else f"{base}/{quote(task_id, safe='')}"


def normalized(task):
    due = task.get("due")
    title = task["title"]
    notes = task.get("notes", "")
    if NOTE_PREFIX in notes:
        try:
            stored = json.loads(notes.rsplit(NOTE_PREFIX, 1)[1])
            expected_date = stored["deadline"][:10] if stored["deadline"] else None
            if expected_date == (due[:10] if due else None) and stored["title"] == title:
                return {**stored, "provider_id": task["id"], "status": "pending" if task.get("status") != "completed" else "completed"}
        except (ValueError, KeyError, TypeError):
            pass
    return {"id": task["id"], "provider_id": task["id"], "title": title,
            "deadline": due[:10] if due else None,
            "due_date_only": bool(due), "status": "pending" if task.get("status") != "completed" else "completed",
            "estimated_minutes": None, "priority": "medium"}


class GoogleTasks:
    def __init__(self, connection=None):
        self.connection = connection or TasksConnection()
        self.journal = Store("google-task-journal", [])

    def records(self, profile):
        items = []
        params = {"maxResults": 100, "showCompleted": "false", "showHidden": "false"}
        seen = set()
        while True:
            page = self.connection.request(profile, "GET", task_path(profile), params=dict(params))
            items.extend(normalized(row) for row in page.get("items", []))
            token = page.get("nextPageToken")
            if not token:
                return sorted(items, key=lambda item: item["id"])
            if token in seen:
                raise GoogleError(502, "Google Tasks repeated a page token.")
            seen.add(token)
            params["pageToken"] = token

    def list(self):
        return self.records(self.connection.profile())

    def get(self, task_id):
        profile = self.connection.profile()
        with closing(self.journal.connect()) as db:
            row = db.execute("SELECT data FROM records WHERE id = ?", (f"{profile['connection_id']}:{task_id}",)).fetchone()
        if row:
            entry = json.loads(row[0])
            if provider_id := entry.get("provider_id"):
                return normalized(self.connection.request(profile, "GET", task_path(profile, provider_id)))
        task = next((item for item in self.records(profile) if item["id"] == task_id), None)
        if task is not None:
            return task
        raise HTTPException(404, "Task not found in the selected Google Tasks list")

    def create(self, approval, data):
        verify_approval("task", approval, data)
        if approval["expires"] <= time.time():
            raise HTTPException(403, "Approval expired")
        profile = self.connection.profile()
        identifier = approval["proposal_id"]
        deadline = data["deadline"]
        due_date = (date.fromisoformat(deadline) if len(deadline) == 10 else aware(deadline).date()) if deadline else None
        record = {"id": identifier, **data}
        journal_key = f"{profile['connection_id']}:{identifier}"
        with closing(self.journal.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM records WHERE id = ?", (journal_key,)).fetchone()
            records = self.records(profile)
            existing = next((item for item in records if item["id"] == identifier), None)
            if existing:
                if any(existing.get(key) != value for key, value in record.items()):
                    raise HTTPException(409, "Existing Google task differs from this proposal")
                return existing
            if row:
                raise HTTPException(409, "The earlier Google Tasks write could not be confirmed. No retry was attempted.")
            if version(records) != approval["snapshot"]:
                raise HTTPException(409, "Approved Tasks snapshot is stale")
            if approval["expires"] <= time.time():
                raise HTTPException(403, "Approval expired")
            db.execute("INSERT INTO records VALUES (?, ?)", (journal_key, canonical({"record": record})))

        notes = f"Exact Plan B deadline: {data['deadline']}" if deadline else "Created with Plan B."
        if data.get("source_quote"):
            notes += "\nSource: " + data["source_quote"]
        if data.get("source_url"):
            notes += "\n" + data["source_url"]
        body = {"title": data["title"], "notes": notes + NOTE_PREFIX + canonical(record)}
        if due_date:
            body["due"] = f"{due_date}T00:00:00Z"
        written = self.connection.request(profile, "POST", task_path(profile), json=body)
        with closing(self.journal.connect()) as db, db:
            db.execute("UPDATE records SET data = ? WHERE id = ?", (canonical({"provider_id": written["id"]}), journal_key))
        return normalized(written)


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("connect", "status", "disconnect"))
    parser.add_argument("--client", default=setting("PLAN_B_GOOGLE_TASKS_CLIENT_FILE") or setting("PLAN_B_GOOGLE_CLIENT_FILE"))
    parser.add_argument("--list-id", default=setting("PLAN_B_GOOGLE_TASK_LIST_ID", "@default"))
    args = parser.parse_args()
    connection = TasksConnection()
    try:
        vault = connection.vault()
        existing = vault.load()
        if args.command == "status":
            print("Google Tasks connection present." if existing else "Google Tasks is disconnected.")
        elif args.command == "disconnect":
            if existing:
                vault.clear()
            print("Local Google Tasks connection removed.")
        elif existing:
            raise GoogleError(409, "Disconnect the current Google Tasks profile before selecting another list.")
        elif not args.client:
            parser.error("Set PLAN_B_GOOGLE_TASKS_CLIENT_FILE or PLAN_B_GOOGLE_CLIENT_FILE in .env.")
        else:
            credentials = desktop_credentials(str(PROJECT_ROOT / Path(args.client).expanduser()), connection.scope)
            selected = google_request(credentials, "GET", f"/users/@me/lists/{quote(args.list_id, safe='')}",
                                      api=connection.api, service=connection.service, reconnect=connection.reconnect)
            vault.save({"connection_id": uuid.uuid4().hex, "list_id": selected["id"], "credentials": json.loads(credentials.to_json())})
            print("Connected to the selected Google Tasks list.")
    except (GoogleError, ImportError) as error:
        parser.exit(1, f"{error}\n" if isinstance(error, GoogleError) else "Install the optional Google dependencies first.\n")


if __name__ == "__main__":
    main()
