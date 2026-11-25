"""SQLite records and scoped authorization for the fictional write APIs."""

import hashlib
import hmac
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from services.config import PROJECT_ROOT, setting


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def version(records):
    return hashlib.sha256(canonical(records).encode()).hexdigest()


def signature(secret, scope, proposal_id, expires, snapshot, data):
    message = canonical([scope, proposal_id, expires, snapshot, data])
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def verify_approval(scope, approval, data):
    secret = setting("PLAN_B_APPROVAL_KEY", "")
    expected = signature(secret, scope, approval["proposal_id"], approval["expires"], approval["snapshot"], data)
    if len(secret) < 32 or not hmac.compare_digest(expected.encode(), approval["approval_signature"].encode()):
        raise HTTPException(403, "Valid human approval is required")


def aware(value):
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.utcoffset() is not None:
            return timestamp
    except ValueError:
        pass
    raise HTTPException(422, "Expected an ISO 8601 date and time with an explicit UTC offset")


class Store:
    def __init__(self, scope, fixtures):
        self.scope = scope
        self.fixtures = fixtures

    def connect(self):
        directory = Path(setting("PLAN_B_STATE_DIR", str(PROJECT_ROOT / ".plan-b")))
        directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(directory / f"{self.scope}.sqlite3")
        db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        return db

    def records(self, db):
        saved = db.execute("SELECT data FROM records ORDER BY id")
        return [*self.fixtures, *(json.loads(row[0]) for row in saved)]

    def list(self):
        with closing(self.connect()) as db:
            return self.records(db)

    def get(self, record_id):
        for record in self.list():
            if record["id"] == record_id:
                return record
        raise HTTPException(404, "Record not found")

    def create(self, approval, data, validate):
        verify_approval(self.scope, approval, data)
        proposal_id = approval["proposal_id"]
        expires = approval["expires"]
        snapshot = approval["snapshot"]
        record = {"id": proposal_id, **data}
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if expires <= time.time():
                raise HTTPException(403, "Approval expired")
            previous = db.execute("SELECT data FROM records WHERE id = ?", (proposal_id,)).fetchone()
            if previous:
                if json.loads(previous[0]) != record:
                    raise HTTPException(409, "Approval was already used for a different record")
                return record
            records = self.records(db)
            if version(records) != snapshot:
                raise HTTPException(409, "Approved state is stale")
            validate(data, records)
            db.execute("INSERT INTO records VALUES (?, ?)", (proposal_id, canonical(record)))
            return record
