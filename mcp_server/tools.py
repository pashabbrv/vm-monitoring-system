from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

import httpx
from pydantic import BaseModel, Field

SERVER_URL = os.getenv("SERVER_URL", "http://server:8000")
ALLOWED_METRICS = {"cpu_percent", "memory_percent", "disk_percent", "net_sent_mb", "net_recv_mb"}


class HostOut(BaseModel):
    host_id: str
    name: str
    status: str
    last_seen: datetime | None = None
    os: str | None = None
    cpu_count: int | None = None
    memory_gb: float | None = None


class MetricPointOut(BaseModel):
    ts: datetime
    value: float


class MetricSeriesOut(BaseModel):
    host_id: str
    metric: str
    points: list[MetricPointOut]
    stats: dict[str, float]


def _parse_time(value: str, now: datetime) -> datetime:
    value = value.strip().lower()
    if value == "now":
        return now
    if value.endswith("m"):
        return now - timedelta(minutes=int(value[:-1]))
    if value.endswith("h"):
        return now - timedelta(hours=int(value[:-1]))
    if value.endswith("d"):
        return now - timedelta(days=int(value[:-1]))
    return datetime.fromisoformat(value)


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = int(0.95 * (len(sorted_vals) - 1))
    return float(sorted_vals[idx])


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = mean(values)
    variance = sum((v - avg) ** 2 for v in values) / len(values)
    return variance ** 0.5


async def _get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(base_url=SERVER_URL, timeout=timeout) as client:
        response = await client.get(path, params=params)
        response.raise_for_status()
        return response.json()


async def list_hosts_impl() -> list[dict]:
    hosts = await _get_json("/api/hosts")
    validated: list[HostOut] = []
    for h in hosts:
        metadata = h.get("metadata", {})
        validated.append(
            HostOut(
                host_id=h["host_id"],
                name=h["name"],
                status=h["status"],
                last_seen=h.get("last_seen"),
                os=metadata.get("os"),
                cpu_count=metadata.get("cpu_count"),
                memory_gb=metadata.get("total_memory_gb"),
            )
        )
    return [v.model_dump(mode="json") for v in validated]


async def get_metrics_impl(host_id: str, metric: str, since: str = "5m", until: str = "now") -> dict:
    if metric not in ALLOWED_METRICS:
        raise ValueError(f"Unsupported metric: {metric}")
    now = datetime.now(timezone.utc)
    since_dt = _parse_time(since, now)
    until_dt = _parse_time(until, now)
    data = await _get_json(
        "/api/metrics",
        params={
            "host_id": host_id,
            "metric": metric,
            "since": since_dt.isoformat(),
            "until": until_dt.isoformat(),
        },
    )
    points = [MetricPointOut(ts=p["ts"], value=float(p["value"])) for p in data.get("points", [])]
    values = [p.value for p in points]
    stats = {
        "min": float(min(values)) if values else 0.0,
        "max": float(max(values)) if values else 0.0,
        "avg": float(mean(values)) if values else 0.0,
        "p95": _p95(values),
    }
    return MetricSeriesOut(host_id=host_id, metric=metric, points=points, stats=stats).model_dump(mode="json")


async def get_top_processes_impl(host_id: str, by: str = "cpu", n: int = 10) -> list[dict]:
    by = by.lower()
    if by not in {"cpu", "memory"}:
        raise ValueError("Parameter 'by' must be either 'cpu' or 'memory'")
    data = await _get_json(f"/api/processes/{host_id}", params={"by": by, "n": n})
    return data.get("processes", [])


async def get_recent_logs_impl(
    host_id: str,
    service: str | None = None,
    lines: int = 100,
    level: str | None = None,
) -> list[dict]:
    data = await _get_json(
        f"/api/logs/{host_id}",
        params={"service": service, "lines": lines, "level": level},
    )
    return data.get("logs", [])


async def get_active_alerts_impl(host_id: str | None = None) -> list[dict]:
    return await _get_json("/api/alerts", params={"active_only": True, "host_id": host_id})


async def _get_recent_traces(host_id: str, limit: int = 20) -> list[dict]:
    return await _get_json(f"/api/traces/{host_id}", params={"limit": limit})


async def get_recent_traces_impl(host_id: str, limit: int = 50) -> list[dict]:
    lim = max(1, min(int(limit), 1000))
    return await _get_json(f"/api/traces/{host_id}", params={"limit": lim})


async def get_trace_by_id_impl(host_id: str, trace_id: str) -> list[dict]:
    return await _get_json(f"/api/traces/{host_id}", params={"trace_id": trace_id.strip()})


async def compare_hosts_impl(host_ids: list[str], metric: str, since: str = "15m") -> dict:
    if metric not in ALLOWED_METRICS:
        raise ValueError(f"Unsupported metric: {metric}")
    hosts = {h["host_id"]: h for h in await list_hosts_impl()}
    aggregated: list[dict[str, Any]] = []
    avgs: list[float] = []
    for host_id in host_ids:
        series = await get_metrics_impl(host_id=host_id, metric=metric, since=since, until="now")
        points = series.get("points", [])
        values = [float(p["value"]) for p in points]
        current = float(values[-1]) if values else 0.0
        avg = float(mean(values)) if values else 0.0
        max_v = float(max(values)) if values else 0.0
        avgs.append(avg)
        aggregated.append(
            {
                "host_id": host_id,
                "name": hosts.get(host_id, {}).get("name", host_id),
                "avg": avg,
                "max": max_v,
                "current": current,
            }
        )
    group_mean = float(mean(avgs)) if avgs else 0.0
    sigma = _std(avgs)
    anomalies = [item["host_id"] for item in aggregated if sigma > 0 and abs(item["avg"] - group_mean) > 2 * sigma]
    return {"metric": metric, "since": since, "hosts": aggregated, "anomalies": anomalies}


async def diagnose_host_impl(host_id: str) -> dict:
    hosts = {h["host_id"]: h for h in await list_hosts_impl()}
    metrics = {}
    for metric in ("cpu_percent", "memory_percent", "disk_percent", "net_sent_mb", "net_recv_mb"):
        series = await get_metrics_impl(host_id=host_id, metric=metric, since="15m", until="now")
        points = series["points"]
        metrics[metric] = points[-1]["value"] if points else 0.0
    top_cpu = await get_top_processes_impl(host_id=host_id, by="cpu", n=5)
    top_memory = await get_top_processes_impl(host_id=host_id, by="memory", n=5)
    alerts = await get_active_alerts_impl(host_id=host_id)
    recent_errors = await get_recent_logs_impl(host_id=host_id, lines=30, level="ERROR")
    traces = await _get_recent_traces(host_id=host_id, limit=20)
    cpu_state = "normal" if metrics["cpu_percent"] < 85 else "high"
    memory_state = "normal" if metrics["memory_percent"] < 90 else "high"
    disk_state = "normal" if metrics["disk_percent"] < 90 else "high"
    incident_confirmed = bool(
        alerts
        or recent_errors
        or metrics["cpu_percent"] >= 85
        or metrics["memory_percent"] >= 90
        or metrics["disk_percent"] >= 90
    )
    top_proc_name = top_cpu[0]["name"] if top_cpu else "unknown"
    summary_text = (
        f"CPU is {cpu_state} ({metrics['cpu_percent']:.1f}%), "
        f"memory is {memory_state} ({metrics['memory_percent']:.1f}%), "
        f"disk is {disk_state} ({metrics['disk_percent']:.1f}%), "
        f"top CPU process is {top_proc_name}."
    )
    return {
        "host": hosts.get(host_id),
        "current_metrics": metrics,
        "top_cpu_processes": top_cpu,
        "top_memory_processes": top_memory,
        "active_alerts": alerts,
        "recent_errors_in_logs": recent_errors,
        "recent_traces": traces,
        "incident_confirmed": incident_confirmed,
        "summary_text": summary_text,
    }
