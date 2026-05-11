from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from server.models import AlertRecord, MetricName

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/monitoring.db")
USE_POSTGRES = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _from_iso(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str):
        return datetime.fromisoformat(ts)
    return None


def _sql(query: str) -> str:
    return query.replace("?", "%s") if USE_POSTGRES else query


@contextmanager
def get_conn() -> Any:
    if USE_POSTGRES:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
        return
    db_path = Path(DATABASE_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _exec(conn: Any, query: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
    return conn.execute(_sql(query), params)


def init_db() -> None:
    if USE_POSTGRES:
        with get_conn() as conn:
            _exec(conn, "CREATE TABLE IF NOT EXISTS hosts (id TEXT PRIMARY KEY, name TEXT NOT NULL, last_seen TIMESTAMPTZ, metadata JSONB)")
            _exec(conn, "CREATE TABLE IF NOT EXISTS metrics (host_id TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL, metric TEXT NOT NULL, value DOUBLE PRECISION NOT NULL, PRIMARY KEY (host_id, ts, metric))")
            _exec(conn, "CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts)")
            _exec(conn, "CREATE INDEX IF NOT EXISTS idx_metrics_host_metric ON metrics(host_id, metric, ts)")
            _exec(conn, "CREATE TABLE IF NOT EXISTS processes (host_id TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL, pid INTEGER NOT NULL, name TEXT NOT NULL, cpu_percent DOUBLE PRECISION, memory_mb DOUBLE PRECISION, PRIMARY KEY (host_id, ts, pid))")
            _exec(conn, "CREATE TABLE IF NOT EXISTS logs (id BIGSERIAL PRIMARY KEY, host_id TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL, service TEXT, level TEXT, message TEXT NOT NULL)")
            _exec(conn, "CREATE INDEX IF NOT EXISTS idx_logs_host_ts ON logs(host_id, ts DESC)")
            _exec(conn, "CREATE TABLE IF NOT EXISTS alerts (id BIGSERIAL PRIMARY KEY, host_id TEXT NOT NULL, metric TEXT NOT NULL, threshold DOUBLE PRECISION NOT NULL, value DOUBLE PRECISION NOT NULL, severity TEXT NOT NULL, started_at TIMESTAMPTZ NOT NULL, resolved_at TIMESTAMPTZ)")
            _exec(conn, "CREATE TABLE IF NOT EXISTS traces (id BIGSERIAL PRIMARY KEY, host_id TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL, trace_id TEXT NOT NULL, span_id TEXT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL, duration_ms DOUBLE PRECISION NOT NULL, attributes JSONB)")
            _exec(conn, "CREATE INDEX IF NOT EXISTS idx_traces_host_ts ON traces(host_id, ts DESC)")
            _exec(conn, "CREATE TABLE IF NOT EXISTS agent_configs (host_id TEXT PRIMARY KEY, interval_seconds INTEGER NOT NULL DEFAULT 5, process_top_n INTEGER NOT NULL DEFAULT 20, log_lines_limit INTEGER NOT NULL DEFAULT 20, enabled BOOLEAN NOT NULL DEFAULT TRUE, updated_at TIMESTAMPTZ NOT NULL)")
        return
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS hosts (id TEXT PRIMARY KEY, name TEXT NOT NULL, last_seen TIMESTAMP, metadata TEXT);
            CREATE TABLE IF NOT EXISTS metrics (host_id TEXT NOT NULL, ts TIMESTAMP NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL, PRIMARY KEY (host_id, ts, metric));
            CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);
            CREATE INDEX IF NOT EXISTS idx_metrics_host_metric ON metrics(host_id, metric, ts);
            CREATE TABLE IF NOT EXISTS processes (host_id TEXT NOT NULL, ts TIMESTAMP NOT NULL, pid INTEGER NOT NULL, name TEXT NOT NULL, cpu_percent REAL, memory_mb REAL, PRIMARY KEY (host_id, ts, pid));
            CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, host_id TEXT NOT NULL, ts TIMESTAMP NOT NULL, service TEXT, level TEXT, message TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_logs_host_ts ON logs(host_id, ts DESC);
            CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, host_id TEXT NOT NULL, metric TEXT NOT NULL, threshold REAL NOT NULL, value REAL NOT NULL, severity TEXT NOT NULL, started_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP);
            CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY AUTOINCREMENT, host_id TEXT NOT NULL, ts TIMESTAMP NOT NULL, trace_id TEXT NOT NULL, span_id TEXT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL, duration_ms REAL NOT NULL, attributes TEXT);
            CREATE INDEX IF NOT EXISTS idx_traces_host_ts ON traces(host_id, ts DESC);
            CREATE TABLE IF NOT EXISTS agent_configs (host_id TEXT PRIMARY KEY, interval_seconds INTEGER NOT NULL DEFAULT 5, process_top_n INTEGER NOT NULL DEFAULT 20, log_lines_limit INTEGER NOT NULL DEFAULT 20, enabled INTEGER NOT NULL DEFAULT 1, updated_at TIMESTAMP NOT NULL);
            """
        )


def register_host(host_id: str, host_name: str, metadata: dict[str, Any]) -> None:
    with get_conn() as conn:
        now_iso = _to_iso(utcnow())
        _exec(conn, "INSERT INTO hosts (id, name, last_seen, metadata) VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name, last_seen=EXCLUDED.last_seen, metadata=EXCLUDED.metadata", (host_id, host_name, now_iso, json.dumps(metadata, ensure_ascii=False)))
        _exec(conn, "INSERT INTO agent_configs (host_id, interval_seconds, process_top_n, log_lines_limit, enabled, updated_at) VALUES (?, 5, 20, 20, ?, ?) ON CONFLICT(host_id) DO NOTHING", (host_id, True if USE_POSTGRES else 1, now_iso))


def touch_host(host_id: str, ts: datetime) -> None:
    with get_conn() as conn:
        _exec(conn, "UPDATE hosts SET last_seen = ? WHERE id = ?", (_to_iso(ts), host_id))


def insert_ingest(host_id: str, ts: datetime, metrics: dict[str, float], processes: list[dict[str, Any]], logs: list[dict[str, Any]], traces: list[dict[str, Any]]) -> None:
    with get_conn() as conn:
        for metric, value in metrics.items():
            _exec(conn, "INSERT INTO metrics (host_id, ts, metric, value) VALUES (?, ?, ?, ?) ON CONFLICT(host_id, ts, metric) DO UPDATE SET value=EXCLUDED.value", (host_id, _to_iso(ts), metric, float(value)))
        for proc in processes:
            _exec(conn, "INSERT INTO processes (host_id, ts, pid, name, cpu_percent, memory_mb) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(host_id, ts, pid) DO UPDATE SET name=EXCLUDED.name, cpu_percent=EXCLUDED.cpu_percent, memory_mb=EXCLUDED.memory_mb", (host_id, _to_iso(ts), int(proc["pid"]), str(proc["name"]), float(proc.get("cpu_percent", 0.0)), float(proc.get("memory_mb", 0.0))))
        for log in logs:
            _exec(conn, "INSERT INTO logs (host_id, ts, service, level, message) VALUES (?, ?, ?, ?, ?)", (host_id, _to_iso(log["ts"]), log.get("service"), log.get("level", "INFO"), log["message"]))
        for trace in traces:
            _exec(conn, "INSERT INTO traces (host_id, ts, trace_id, span_id, operation, status, duration_ms, attributes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (host_id, _to_iso(trace["ts"]), trace["trace_id"], trace["span_id"], trace["operation"], trace["status"], float(trace["duration_ms"]), json.dumps(trace.get("attributes", {}), ensure_ascii=False)))
        _exec(conn, "UPDATE hosts SET last_seen = ? WHERE id = ?", (_to_iso(ts), host_id))


def list_hosts() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = _exec(conn, "SELECT id, name, last_seen, metadata FROM hosts ORDER BY name ASC").fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        meta = row["metadata"] if isinstance(row["metadata"], dict) else (json.loads(row["metadata"]) if row["metadata"] else {})
        out.append({"host_id": row["id"], "name": row["name"], "last_seen": _from_iso(row["last_seen"]), "metadata": meta})
    return out


def get_metrics(host_id: str, metric: MetricName, since: datetime | None = None, until: datetime | None = None) -> list[dict[str, Any]]:
    query = "SELECT ts, value FROM metrics WHERE host_id = ? AND metric = ?"
    params: list[Any] = [host_id, metric]
    if since:
        query += " AND ts >= ?"
        params.append(_to_iso(since))
    if until:
        query += " AND ts <= ?"
        params.append(_to_iso(until))
    query += " ORDER BY ts ASC"
    with get_conn() as conn:
        rows = _exec(conn, query, params).fetchall()
    return [{"ts": _from_iso(r["ts"]), "value": float(r["value"])} for r in rows]


def get_latest_processes(host_id: str, by: str = "cpu", n: int = 10) -> tuple[datetime | None, list[dict[str, Any]]]:
    column = "cpu_percent" if by == "cpu" else "memory_mb"
    with get_conn() as conn:
        latest_row = _exec(conn, "SELECT MAX(ts) AS latest_ts FROM processes WHERE host_id = ?", (host_id,)).fetchone()
        if not latest_row or latest_row["latest_ts"] is None:
            return None, []
        latest_ts = latest_row["latest_ts"]
        rows = _exec(conn, f"SELECT pid, name, cpu_percent, memory_mb FROM processes WHERE host_id = ? AND ts = ? ORDER BY {column} DESC LIMIT ?", (host_id, latest_ts, n)).fetchall()
    return _from_iso(latest_ts), [dict(row) for row in rows]


def get_recent_logs(host_id: str, service: str | None = None, level: str | None = None, lines: int = 100) -> list[dict[str, Any]]:
    query = "SELECT ts, service, level, message FROM logs WHERE host_id = ?"
    params: list[Any] = [host_id]
    if service:
        query += " AND service = ?"
        params.append(service)
    if level:
        query += " AND level = ?"
        params.append(level.upper())
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(lines)
    with get_conn() as conn:
        rows = _exec(conn, query, params).fetchall()
    return [{"ts": _from_iso(row["ts"]), "service": row["service"], "level": row["level"], "message": row["message"]} for row in rows]


def create_alert(host_id: str, metric: str, threshold: float, value: float, severity: str, started_at: datetime) -> int:
    with get_conn() as conn:
        query = "INSERT INTO alerts (host_id, metric, threshold, value, severity, started_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, NULL)"
        if USE_POSTGRES:
            row = _exec(conn, query + " RETURNING id", (host_id, metric, threshold, value, severity, _to_iso(started_at))).fetchone()
            return int(row["id"])
        cur = _exec(conn, query, (host_id, metric, threshold, value, severity, _to_iso(started_at)))
        return int(cur.lastrowid)


def update_alert_value(alert_id: int, value: float) -> None:
    with get_conn() as conn:
        _exec(conn, "UPDATE alerts SET value = ? WHERE id = ?", (value, alert_id))


def resolve_alerts(host_id: str, metric: str, threshold: float, severity: str, resolved_at: datetime) -> None:
    with get_conn() as conn:
        _exec(conn, "UPDATE alerts SET resolved_at = ? WHERE host_id = ? AND metric = ? AND threshold = ? AND severity = ? AND resolved_at IS NULL", (_to_iso(resolved_at), host_id, metric, threshold, severity))


def get_active_alert(host_id: str, metric: str, threshold: float, severity: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = _exec(conn, "SELECT id, host_id, metric, threshold, value, severity, started_at, resolved_at FROM alerts WHERE host_id = ? AND metric = ? AND threshold = ? AND severity = ? AND resolved_at IS NULL ORDER BY started_at DESC LIMIT 1", (host_id, metric, threshold, severity)).fetchone()
    return dict(row) if row else None


def list_alerts(active_only: bool = True, host_id: str | None = None) -> list[AlertRecord]:
    query = "SELECT id, host_id, metric, threshold, value, severity, started_at, resolved_at FROM alerts WHERE 1=1"
    params: list[Any] = []
    if active_only:
        query += " AND resolved_at IS NULL"
    if host_id:
        query += " AND host_id = ?"
        params.append(host_id)
    query += " ORDER BY started_at DESC"
    with get_conn() as conn:
        rows = _exec(conn, query, params).fetchall()
    return [AlertRecord(id=int(row["id"]), host_id=row["host_id"], metric=row["metric"], threshold=float(row["threshold"]), value=float(row["value"]), severity=row["severity"], started_at=_from_iso(row["started_at"]), resolved_at=_from_iso(row["resolved_at"])) for row in rows]


def get_agent_config(host_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = _exec(conn, "SELECT host_id, interval_seconds, process_top_n, log_lines_limit, enabled, updated_at FROM agent_configs WHERE host_id = ?", (host_id,)).fetchone()
    if not row:
        return None
    return {"host_id": row["host_id"], "interval_seconds": int(row["interval_seconds"]), "process_top_n": int(row["process_top_n"]), "log_lines_limit": int(row["log_lines_limit"]), "enabled": bool(row["enabled"]), "updated_at": _from_iso(row["updated_at"])}


def upsert_agent_config(host_id: str, interval_seconds: int, process_top_n: int, log_lines_limit: int, enabled: bool) -> None:
    with get_conn() as conn:
        _exec(conn, "INSERT INTO agent_configs (host_id, interval_seconds, process_top_n, log_lines_limit, enabled, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(host_id) DO UPDATE SET interval_seconds=EXCLUDED.interval_seconds, process_top_n=EXCLUDED.process_top_n, log_lines_limit=EXCLUDED.log_lines_limit, enabled=EXCLUDED.enabled, updated_at=EXCLUDED.updated_at", (host_id, interval_seconds, process_top_n, log_lines_limit, enabled if USE_POSTGRES else (1 if enabled else 0), _to_iso(utcnow())))


def get_recent_traces(host_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = _exec(conn, "SELECT ts, trace_id, span_id, operation, status, duration_ms, attributes FROM traces WHERE host_id = ? ORDER BY ts DESC LIMIT ?", (host_id, limit)).fetchall()
    return _rows_to_trace_dicts(rows)


def get_traces_by_trace_id(host_id: str, trace_id: str) -> list[dict[str, Any]]:
    tid = trace_id.strip()
    if not tid:
        return []
    with get_conn() as conn:
        rows = _exec(conn, "SELECT ts, trace_id, span_id, operation, status, duration_ms, attributes FROM traces WHERE host_id = ? AND trace_id = ? ORDER BY ts DESC", (host_id, tid)).fetchall()
    return _rows_to_trace_dicts(rows)


def _rows_to_trace_dicts(rows: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        attrs = row["attributes"] if isinstance(row["attributes"], dict) else (json.loads(row["attributes"]) if row["attributes"] else {})
        result.append({"ts": _from_iso(row["ts"]), "trace_id": row["trace_id"], "span_id": row["span_id"], "operation": row["operation"], "status": row["status"], "duration_ms": float(row["duration_ms"]), "attributes": attrs})
    return result
