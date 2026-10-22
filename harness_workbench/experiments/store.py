"""Append-only experiment records and content-addressed artifacts in SQLite.

Each operation is a transaction. No run, decision, manifest, or promotion is
updated in place. A repeated experiment gets a new UUID, not a reused event log.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from harness_workbench.experiments.models import canonical, digest


class ExperimentStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "experiments.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, manifest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
                    variant_id TEXT NOT NULL, task_id TEXT NOT NULL, repetition INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(experiment_id, variant_id, task_id, repetition));
                CREATE TABLE IF NOT EXISTS decisions (
                    experiment_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS objects (
                    hash TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS promotions (
                    experiment_id TEXT PRIMARY KEY, object_hash TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, manifest: dict) -> str:
        experiment_id = "exp-" + uuid4().hex
        with self.connection() as db:
            db.execute("INSERT INTO experiments VALUES (?, ?, ?)",
                       (experiment_id, digest(manifest), canonical(manifest)))
        return experiment_id

    def event(self, experiment_id: str, kind: str, payload: dict) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO events(experiment_id, kind, payload) VALUES (?, ?, ?)",
                       (experiment_id, kind, canonical(payload)))

    def record(self, experiment_id: str, row: dict) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                       (row["run_id"], experiment_id, row["variant_id"], row["task_id"], row["repetition"], canonical(row)))

    def records(self, experiment_id: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM runs WHERE experiment_id=? ORDER BY rowid", (experiment_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def finish(self, experiment_id: str, result: dict, *, harness: dict | None = None) -> None:
        if harness is not None and not result.get("promote"):
            raise ValueError("a rejected decision cannot publish a harness")
        with self.connection() as db:
            db.execute("INSERT INTO decisions VALUES (?, ?)", (experiment_id, canonical(result)))
            if harness is not None:
                object_hash = digest(harness)
                db.execute("INSERT OR IGNORE INTO objects VALUES (?, ?)", (object_hash, canonical(harness)))
                db.execute("INSERT INTO promotions VALUES (?, ?)", (experiment_id, object_hash))

    def promote(self, experiment_id: str, harness: dict) -> str:
        object_hash = digest(harness)
        with self.connection() as db:
            decision = db.execute("SELECT payload FROM decisions WHERE experiment_id=?", (experiment_id,)).fetchone()
            if not decision or not json.loads(decision[0]).get("promote"):
                raise ValueError("an accepted decision is required before promotion")
            db.execute("INSERT OR IGNORE INTO objects VALUES (?, ?)", (object_hash, canonical(harness)))
            db.execute("INSERT INTO promotions VALUES (?, ?)", (experiment_id, object_hash))
        return object_hash

    def inspect(self, experiment_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT manifest_hash, manifest FROM experiments WHERE id=?", (experiment_id,)).fetchone()
            if not row:
                raise ValueError(f"unknown experiment: {experiment_id}")
            decision = db.execute("SELECT payload FROM decisions WHERE experiment_id=?", (experiment_id,)).fetchone()
            events = db.execute("SELECT kind, payload FROM events WHERE experiment_id=? ORDER BY seq", (experiment_id,)).fetchall()
            promoted = db.execute("SELECT object_hash FROM promotions WHERE experiment_id=?", (experiment_id,)).fetchone()
        manifest = json.loads(row[1])
        if digest(manifest) != row[0]:
            raise ValueError("manifest integrity check failed")
        return {"experiment_id": experiment_id, "manifest_hash": row[0], "manifest": manifest,
                "decision": json.loads(decision[0]) if decision else None,
                "events": [{"kind": kind, "payload": json.loads(payload)} for kind, payload in events],
                "promoted_object": promoted[0] if promoted else None}

    def list(self) -> list[str]:
        with self.connection() as db:
            return [row[0] for row in db.execute("SELECT id FROM experiments ORDER BY rowid DESC")]

    def object(self, object_hash: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM objects WHERE hash=?", (object_hash,)).fetchone()
        if row is None:
            raise ValueError("unknown harness object")
        value = json.loads(row[0])
        if digest(value) != object_hash:
            raise ValueError("harness object integrity check failed")
        return value
