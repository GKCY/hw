#!/usr/bin/env python3
"""SQLite-WAL plus atomic-sidecar state for resumable Batch-100 work."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from common import BatchError, STATE_ORDER, atomic_json, require_hash


ALLOWED_TRANSITIONS = {
    "PLANNED": {"PROBE_ELIGIBLE"},
    "PROBE_ELIGIBLE": {"BOUND"},
    "BOUND": {"CALIBRATING", "PROBE_ELIGIBLE"},
    "CALIBRATING": {"FROZEN", "PROBE_ELIGIBLE"},
    "FROZEN": {"REPLAYING"},
    "REPLAYING": {"VALIDATED"},
    "VALIDATED": {"FINALIZED"},
    "FINALIZED": set(),
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.path = run_dir / "state.sqlite3"
        self.sidecars = run_dir / "state"

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            connection.close()
            raise BatchError(f"SQLite refused WAL mode: {mode}")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(
        self,
        case_ids: list[str],
        input_hashes: Mapping[str, str],
        baseline: Mapping[str, Any],
    ) -> None:
        if self.path.exists():
            raise BatchError(f"run is already initialized: {self.run_dir}")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.sidecars.mkdir(parents=True, exist_ok=True)
        for key, value in input_hashes.items():
            require_hash(value, f"input_hashes.{key}")
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE cases (
                    case_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    slot TEXT,
                    binding_sha256 TEXT,
                    injection_sha256 TEXT,
                    repair_sha256 TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    old_state TEXT,
                    new_state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(case_id) REFERENCES cases(case_id)
                );
                CREATE TABLE artifacts (
                    case_id TEXT NOT NULL,
                    logical_name TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    bytes INTEGER NOT NULL,
                    PRIMARY KEY(case_id, logical_name),
                    FOREIGN KEY(case_id) REFERENCES cases(case_id)
                );
                """
            )
            metadata = {
                "schema_version": "mock_lef_batch100.state.v1",
                "created_at": utc_now(),
                "input_hashes": dict(input_hashes),
                "baseline": dict(baseline),
            }
            for key, value in metadata.items():
                connection.execute(
                    "INSERT INTO metadata(key,value_json) VALUES (?,?)",
                    (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
                )
            now = utc_now()
            for case_id in case_ids:
                connection.execute(
                    "INSERT INTO cases(case_id,state,updated_at) VALUES (?,?,?)",
                    (case_id, "PLANNED", now),
                )
                connection.execute(
                    "INSERT INTO events(case_id,old_state,new_state,reason,created_at)"
                    " VALUES (?,?,?,?,?)",
                    (case_id, None, "PLANNED", "init", now),
                )
        for case_id in case_ids:
            self._write_sidecar(case_id)

    def metadata(self) -> dict[str, Any]:
        with self.connect() as connection:
            return {
                row["key"]: json.loads(row["value_json"])
                for row in connection.execute("SELECT key,value_json FROM metadata")
            }

    def verify_inputs(self, current_hashes: Mapping[str, str]) -> None:
        expected = self.metadata().get("input_hashes")
        if expected != dict(current_hashes):
            changed = sorted(
                key
                for key in set(expected or {}) | set(current_hashes)
                if (expected or {}).get(key) != current_hashes.get(key)
            )
            raise BatchError(
                "resume refused because bound inputs changed: " + ", ".join(changed)
            )

    def rows(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute("SELECT * FROM cases ORDER BY case_id")
            ]

    def row(self, case_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM cases WHERE case_id=?", (case_id,)
            ).fetchone()
        if row is None:
            raise BatchError(f"unknown case ID: {case_id}")
        return dict(row)

    def transition(
        self,
        case_id: str,
        new_state: str,
        reason: str,
        **fields: Any,
    ) -> None:
        if new_state not in STATE_ORDER:
            raise BatchError(f"unknown state: {new_state}")
        allowed_fields = {
            "slot",
            "binding_sha256",
            "injection_sha256",
            "repair_sha256",
            "attempt",
        }
        unknown = sorted(set(fields) - allowed_fields)
        if unknown:
            raise BatchError(f"unknown state fields: {', '.join(unknown)}")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT state FROM cases WHERE case_id=?", (case_id,)
            ).fetchone()
            if row is None:
                raise BatchError(f"unknown case ID: {case_id}")
            old_state = row["state"]
            if new_state not in ALLOWED_TRANSITIONS[old_state]:
                raise BatchError(f"illegal state transition {old_state} -> {new_state}")
            assignments = ["state=?", "updated_at=?"]
            values: list[Any] = [new_state, utc_now()]
            for key, value in fields.items():
                if key.endswith("_sha256") and value is not None:
                    require_hash(value, f"{case_id}.{key}")
                assignments.append(f"{key}=?")
                values.append(value)
            values.append(case_id)
            connection.execute(
                f"UPDATE cases SET {','.join(assignments)} WHERE case_id=?", values
            )
            connection.execute(
                "INSERT INTO events(case_id,old_state,new_state,reason,created_at)"
                " VALUES (?,?,?,?,?)",
                (case_id, old_state, new_state, reason, utc_now()),
            )
        self._write_sidecar(case_id)

    def register_artifact(
        self, case_id: str, logical_name: str, relative_path: str, sha256: str, size: int
    ) -> None:
        require_hash(sha256, f"{case_id}.{logical_name}.sha256")
        if size < 0:
            raise BatchError("artifact byte count cannot be negative")
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO artifacts"
                "(case_id,logical_name,relative_path,sha256,bytes) VALUES (?,?,?,?,?)",
                (case_id, logical_name, relative_path, sha256, size),
            )
        # Artifact rows are part of the per-case machine state, so publish the
        # sidecar immediately instead of waiting for an unrelated transition.
        self._write_sidecar(case_id)

    def remove_artifacts(
        self, case_id: str, logical_names: list[str] | tuple[str, ...]
    ) -> int:
        """Idempotently remove transient artifact rows and refresh the sidecar."""

        if not isinstance(logical_names, (list, tuple)) or any(
            not isinstance(name, str) or not name
            for name in logical_names
        ):
            raise BatchError("artifact logical names must be non-empty strings")
        names = sorted(set(logical_names))
        with self.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM cases WHERE case_id=?", (case_id,)
            ).fetchone() is None:
                raise BatchError(f"unknown case ID: {case_id}")
            removed = 0
            if names:
                placeholders = ",".join("?" for _ in names)
                cursor = connection.execute(
                    "DELETE FROM artifacts WHERE case_id=? "
                    f"AND logical_name IN ({placeholders})",
                    (case_id, *names),
                )
                removed = cursor.rowcount
        self._write_sidecar(case_id)
        return removed

    def refresh_sidecar(self, case_id: str) -> None:
        """Rebuild one atomic sidecar from committed SQLite state."""

        self._write_sidecar(case_id)

    def _write_sidecar(self, case_id: str) -> None:
        row = self.row(case_id)
        with self.connect() as connection:
            artifacts = [
                dict(item)
                for item in connection.execute(
                    "SELECT logical_name,relative_path,sha256,bytes FROM artifacts "
                    "WHERE case_id=? ORDER BY logical_name",
                    (case_id,),
                )
            ]
            events = [
                dict(item)
                for item in connection.execute(
                    "SELECT sequence,old_state,new_state,reason,created_at FROM events "
                    "WHERE case_id=? ORDER BY sequence",
                    (case_id,),
                )
            ]
        atomic_json(
            self.sidecars / f"{case_id}.json",
            {
                "schema_version": "mock_lef_batch100.case_state.v1",
                "case": row,
                "artifacts": artifacts,
                "events": events,
            },
            durable=True,
        )
