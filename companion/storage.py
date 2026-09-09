"""Local session history: SQLite metadata and atomic compressed trace files."""
from __future__ import annotations

from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time

from .metrics import MAX_RECORD_BYTES, analyze_record, canonicalize_json, validate_id


class Storage:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.trace_dir = self.data_dir / "traces"
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.data_dir / "history.sqlite3"
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    drill TEXT NOT NULL,
                    benchmark_key TEXT NOT NULL,
                    record_sha256 TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    saved_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_benchmark_time
                    ON sessions(benchmark_key, started_at DESC);
                CREATE TABLE IF NOT EXISTS coaching (
                    record_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                    result_json TEXT NOT NULL,
                    saved_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS coaching_journal (
                    id INTEGER PRIMARY KEY,
                    record_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    result_json TEXT NOT NULL,
                    question TEXT NOT NULL,
                    context_json TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS coaching_journal_record_time
                    ON coaching_journal(record_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS storage_migrations (
                    name TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS sensitivity_experiments (
                    id TEXT PRIMARY KEY,
                    experiment_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
            """)
            # Upgrade only coaching metadata. Existing telemetry and reports are
            # immutable, including the hashes used to verify compressed traces.
            connection.execute("BEGIN IMMEDIATE")
            migration = "coaching_journal_v1"
            if connection.execute("SELECT 1 FROM storage_migrations WHERE name = ?", (migration,)).fetchone() is None:
                connection.execute(
                    "INSERT INTO coaching_journal(record_id, result_json, question, context_json, created_at) "
                    "SELECT record_id, result_json, '', NULL, saved_at FROM coaching"
                )
                connection.execute("INSERT INTO storage_migrations(name) VALUES (?)", (migration,))

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _json(value: object) -> str:
        try:
            return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("Data must contain finite JSON values") from exc

    @staticmethod
    def _limit(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
            raise ValueError("limit must be between 1 and 100")
        return value

    def save_record(self, record: dict) -> dict:
        record = canonicalize_json(record)
        report = analyze_record(record)
        raw = self._json(record).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        record_id = record["id"]
        final_path = self.trace_dir / f"{record_id}.json.gz"
        temp_path = None
        new_file = False
        # BEGIN IMMEDIATE serializes separate processes too. A trace is made
        # durable before its metadata commits. Interrupted writes can leave an
        # unreferenced file, but never a committed session without its trace.
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT record_sha256, report_json FROM sessions WHERE id = ?", (record_id,)
            ).fetchone()
            if existing:
                if existing["record_sha256"] != digest:
                    raise ValueError("Record id already exists with different content")
                if not final_path.is_file():
                    raise RuntimeError("Stored session trace is missing")
                return json.loads(existing["report_json"])
            self._validate_training_links(connection, report)
            try:
                with tempfile.NamedTemporaryFile(dir=self.trace_dir, prefix=".pending-", suffix=".gz", delete=False) as stream:
                    temp_path = Path(stream.name)
                    stream.write(gzip.compress(raw, compresslevel=6, mtime=0))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, final_path)
                temp_path = None
                new_file = True
                # Windows cannot open directory descriptors for fsync. The
                # trace itself is flushed above and replaced atomically on
                # every platform; also persist the directory entry where the
                # OS exposes that operation.
                directory_flag = getattr(os, "O_DIRECTORY", None)
                if directory_flag is not None:
                    directory_fd = os.open(self.trace_dir, os.O_RDONLY | directory_flag)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                connection.execute(
                    "INSERT INTO sessions(id, started_at, drill, benchmark_key, record_sha256, report_json, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (record_id, record["started_at"], record["drill"], report["benchmark_key"], digest,
                     self._json(report), time.time()),
                )
                connection.commit()
                return report
            except Exception:
                connection.rollback()
                if new_file:
                    final_path.unlink(missing_ok=True)
                raise
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)

    @staticmethod
    def _validate_training_links(connection: sqlite3.Connection, report: dict) -> None:
        context = report.get("training_context", {})
        for field in ("baseline_record_id", "source_coaching_record_id"):
            reference = context.get(field)
            if not reference:
                continue
            linked = connection.execute(
                "SELECT started_at, drill, benchmark_key FROM sessions WHERE id = ?", (reference,)
            ).fetchone()
            if linked is None:
                raise ValueError(f"training_context.{field} references an unknown round")
            if linked["started_at"] >= report["started_at"]:
                raise ValueError(f"training_context.{field} must reference an earlier round")
            if field == "baseline_record_id":
                if linked["drill"] != report["drill"]:
                    raise ValueError("A baseline must use the same drill")
                if context.get("kind") == "retest" and linked["benchmark_key"] != report["benchmark_key"]:
                    raise ValueError("A retest must match its baseline settings")

    @staticmethod
    def _summary(report: dict, coaching_available: bool = False) -> dict:
        summary = {k: report[k] for k in ("record_id", "drill", "started_at", "duration_s", "benchmark_key",
                                          "valid", "quality", "metrics")} | {"coaching_available": coaching_available}
        if "training_context" in report:
            summary["training_context"] = report["training_context"]
        if "tracking_motion" in report:
            summary["tracking_motion"] = report["tracking_motion"]
        return summary

    def list_sessions(self, limit: int = 30) -> list[dict]:
        limit = self._limit(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT report_json, EXISTS(SELECT 1 FROM coaching c WHERE c.record_id=s.id) AS coached "
                "FROM sessions s ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._summary(json.loads(row["report_json"]), bool(row["coached"])) for row in rows]

    def baseline_history(self, limit: int = 100) -> list[dict]:
        """Read baseline reports even after ordinary rounds fill recent history."""
        limit = self._limit(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT report_json FROM sessions WHERE json_extract(report_json, '$.training_context.kind') = 'baseline' "
                "ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["report_json"]) for row in rows]

    def get_session(self, record_id: str) -> dict:
        record_id = validate_id(record_id)
        with self._connect() as connection:
            row = connection.execute("SELECT record_sha256 FROM sessions WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(record_id)
        try:
            with gzip.open(self.trace_dir / f"{record_id}.json.gz", "rb") as stream:
                raw = stream.read(MAX_RECORD_BYTES + 1)
            if len(raw) > MAX_RECORD_BYTES:
                raise RuntimeError("Stored trace exceeds size limit")
            if hashlib.sha256(raw).hexdigest() != row["record_sha256"]:
                raise RuntimeError("Stored trace failed integrity verification")
            return json.loads(raw)
        except (OSError, EOFError, ValueError) as exc:
            raise RuntimeError("Stored session trace could not be read") from exc

    def get_report(self, record_id: str) -> dict:
        record_id = validate_id(record_id)
        with self._connect() as connection:
            row = connection.execute("SELECT report_json FROM sessions WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(record_id)
        return json.loads(row["report_json"])

    def save_coaching(self, record_id: str, result: dict, question: str = "", context: dict | None = None) -> None:
        record_id = validate_id(record_id)
        if not isinstance(result, dict):
            raise ValueError("Coaching result must be an object")
        if not isinstance(question, str) or len(question) > 12_000:
            raise ValueError("Coaching question must be a string of at most 12000 characters")
        if context is not None and not isinstance(context, dict):
            raise ValueError("Coaching context must be an object or null")
        encoded = self._json(result)
        encoded_context = self._json(context) if context is not None else None
        if len((encoded + (encoded_context or "") + question).encode("utf-8")) > 1024 * 1024:
            raise ValueError("Coaching entry exceeds the 1 MiB limit")
        with self._lock, self._connect() as connection:
            if connection.execute("SELECT 1 FROM sessions WHERE id = ?", (record_id,)).fetchone() is None:
                raise KeyError(record_id)
            timestamp = time.time()
            connection.execute(
                "INSERT INTO coaching(record_id, result_json, saved_at) VALUES (?, ?, ?) "
                "ON CONFLICT(record_id) DO UPDATE SET result_json=excluded.result_json, saved_at=excluded.saved_at",
                (record_id, encoded, timestamp),
            )
            connection.execute(
                "INSERT INTO coaching_journal(record_id, result_json, question, context_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (record_id, encoded, question, encoded_context, timestamp),
            )

    def get_coaching(self, record_id: str) -> dict | None:
        record_id = validate_id(record_id)
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM sessions WHERE id = ?", (record_id,)).fetchone() is None:
                raise KeyError(record_id)
            row = connection.execute("SELECT result_json FROM coaching WHERE record_id = ?", (record_id,)).fetchone()
        return json.loads(row["result_json"]) if row else None

    def matching_history(self, report: dict, limit: int = 5) -> list[dict]:
        limit = self._limit(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT report_json, EXISTS(SELECT 1 FROM coaching c WHERE c.record_id=s.id) AS coached "
                "FROM sessions s WHERE benchmark_key = ? AND id != ? AND started_at <= ? "
                "ORDER BY started_at DESC, id DESC LIMIT ?",
                (report["benchmark_key"], report["record_id"], report["started_at"], limit),
            ).fetchall()
        return [json.loads(row["report_json"]) | {"coaching_available": bool(row["coached"])} for row in rows]

    def coaching_history(self, report: dict, limit: int = 6) -> list[dict]:
        """Prior advice, including follow-ups and the explicitly linked source round."""
        limit = self._limit(limit)
        source = report.get("training_context", {}).get("source_coaching_record_id") or None
        with self._connect() as connection:
            query = (
                "SELECT j.*, s.benchmark_key, s.drill FROM coaching_journal j "
                "JOIN sessions s ON s.id = j.record_id "
                "WHERE (s.started_at < ? OR s.id = ?) AND (s.drill = ? OR s.id = ?) "
                "ORDER BY j.created_at DESC, j.id DESC LIMIT ?"
            )
            rows = connection.execute(query, (report["started_at"], report["record_id"], report["drill"], source, limit)).fetchall()
            # A busy follow-up conversation must not crowd out the prescription
            # that actually started this training cycle.
            if source and not any(row["record_id"] == source for row in rows):
                linked = connection.execute(
                    "SELECT j.*, s.benchmark_key, s.drill FROM coaching_journal j "
                    "JOIN sessions s ON s.id = j.record_id WHERE s.id = ? AND s.started_at < ? "
                    "ORDER BY j.created_at DESC, j.id DESC LIMIT 1", (source, report["started_at"])
                ).fetchone()
                if linked is not None:
                    rows = rows[:limit - 1] + [linked]
                    rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
        return [{"record_id": row["record_id"], "created_at": row["created_at"],
                 "benchmark_key": row["benchmark_key"], "drill": row["drill"],
                 "result": json.loads(row["result_json"]), "question": row["question"],
                 "context": json.loads(row["context_json"]) if row["context_json"] is not None else None}
                for row in rows]

    def save_sensitivity_experiment(self, experiment: dict) -> None:
        if not isinstance(experiment, dict):
            raise ValueError("Sensitivity experiment must be an object")
        experiment_id = validate_id(experiment.get("id"))
        encoded = self._json(canonicalize_json(experiment))
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Sensitivity experiment exceeds the 1 MiB limit")
        timestamp = time.time()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO sensitivity_experiments(id, experiment_json, created_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET experiment_json=excluded.experiment_json, updated_at=excluded.updated_at",
                (experiment_id, encoded, timestamp, timestamp),
            )

    def get_sensitivity_experiment(self, experiment_id: str) -> dict:
        experiment_id = validate_id(experiment_id)
        with self._connect() as connection:
            row = connection.execute("SELECT experiment_json FROM sensitivity_experiments WHERE id = ?", (experiment_id,)).fetchone()
        if row is None:
            raise KeyError(experiment_id)
        return json.loads(row["experiment_json"])

    def list_sensitivity_experiments(self, limit: int = 10) -> list[dict]:
        limit = self._limit(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT experiment_json FROM sensitivity_experiments ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row["experiment_json"]) for row in rows]
