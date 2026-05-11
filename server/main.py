from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import Body, FastAPI, HTTPException, Query

from server import alerts, storage
from server.models import (
    AgentConfigResponse,
    HealthResponse,
    HostRegistrationRequest,
    HostResponse,
    IngestRequest,
    LogResponse,
    MetricsResponse,
    MetricName,
    ProcessResponse,
    SeedDemoAlertsRequest,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Monitoring Server", version="1.0.0")


@app.on_event("startup")
async def on_startup() -> None:
    storage.init_db()
    db_target = storage.DATABASE_URL if storage.USE_POSTGRES else storage.DATABASE_PATH
    logger.info("Server started, DB initialized: %s", db_target)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/api/hosts/register")
async def register_host(payload: HostRegistrationRequest) -> dict[str, str]:
    storage.register_host(payload.host_id, payload.host_name, payload.metadata.model_dump())
    logger.info("Host registered: %s (%s)", payload.host_name, payload.host_id)
    return {"status": "registered"}


@app.post("/ingest")
async def ingest(payload: IngestRequest) -> dict[str, str]:
    storage.insert_ingest(
        host_id=payload.host_id,
        ts=payload.ts,
        metrics=payload.metrics.model_dump(),
        processes=[p.model_dump() for p in payload.processes],
        logs=[l.model_dump() for l in payload.logs],
        traces=[t.model_dump() for t in payload.traces],
    )
    alerts.evaluate_thresholds(payload.host_id, payload.ts, payload.metrics.model_dump())
    return {"status": "ingested"}


@app.get("/api/hosts", response_model=list[HostResponse])
async def get_hosts() -> list[HostResponse]:
    now = datetime.now(timezone.utc)
    result: list[HostResponse] = []
    for host in storage.list_hosts():
        last_seen = host["last_seen"]
        status = "stale"
        if last_seen and (now - last_seen) <= timedelta(seconds=30):
            status = "online"
        result.append(
            HostResponse(
                host_id=host["host_id"],
                name=host["name"],
                last_seen=last_seen,
                status=status,
                metadata=host["metadata"],
            )
        )
    return result


@app.get("/api/metrics", response_model=MetricsResponse)
async def get_metrics(
    host_id: str,
    metric: MetricName,
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
) -> MetricsResponse:
    points = storage.get_metrics(host_id=host_id, metric=metric, since=since, until=until)
    return MetricsResponse(host_id=host_id, metric=metric, points=points)


@app.get("/api/processes/{host_id}", response_model=ProcessResponse)
async def get_processes(host_id: str, by: Literal["cpu", "memory"] = "cpu", n: int = 10) -> ProcessResponse:
    ts, rows = storage.get_latest_processes(host_id, by=by, n=n)
    return ProcessResponse(host_id=host_id, ts=ts, processes=rows)


@app.get("/api/logs/{host_id}", response_model=LogResponse)
async def get_logs(
    host_id: str,
    service: str | None = None,
    level: str | None = None,
    lines: int = 100,
) -> LogResponse:
    rows = storage.get_recent_logs(host_id=host_id, service=service, level=level, lines=lines)
    return LogResponse(host_id=host_id, logs=rows)


@app.get("/api/alerts")
async def get_alerts(active_only: bool = True, host_id: str | None = None) -> list[dict]:
    alerts_data = storage.list_alerts(active_only=active_only, host_id=host_id)
    now = datetime.now(timezone.utc)
    response: list[dict] = []
    for alert in alerts_data:
        duration = int((now - alert.started_at).total_seconds()) if alert.resolved_at is None else 0
        response.append(
            {
                "id": alert.id,
                "host_id": alert.host_id,
                "metric": alert.metric,
                "threshold": alert.threshold,
                "value": alert.value,
                "severity": alert.severity,
                "started_at": alert.started_at,
                "resolved_at": alert.resolved_at,
                "duration_seconds": duration,
            }
        )
    return response


@app.get("/api/agents/config/{host_id}", response_model=AgentConfigResponse)
async def get_agent_config(host_id: str) -> AgentConfigResponse:
    cfg = storage.get_agent_config(host_id)
    if cfg is None:
        storage.upsert_agent_config(host_id, interval_seconds=5, process_top_n=20, log_lines_limit=20, enabled=True)
        cfg = storage.get_agent_config(host_id)
    return AgentConfigResponse(**cfg)


@app.put("/api/agents/config/{host_id}", response_model=AgentConfigResponse)
async def set_agent_config(
    host_id: str,
    interval_seconds: int = Query(default=5, ge=1, le=120),
    process_top_n: int = Query(default=20, ge=1, le=200),
    log_lines_limit: int = Query(default=20, ge=1, le=1000),
    enabled: bool = True,
) -> AgentConfigResponse:
    storage.upsert_agent_config(
        host_id=host_id,
        interval_seconds=interval_seconds,
        process_top_n=process_top_n,
        log_lines_limit=log_lines_limit,
        enabled=enabled,
    )
    cfg = storage.get_agent_config(host_id)
    return AgentConfigResponse(**cfg)


@app.get("/api/traces/{host_id}")
async def get_traces(
    host_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    trace_id: str | None = Query(default=None),
) -> list[dict]:
    if trace_id and trace_id.strip():
        return storage.get_traces_by_trace_id(host_id, trace_id.strip())
    return storage.get_recent_traces(host_id, limit=limit)


def _dev_seed_enabled() -> bool:
    v = os.getenv("ALLOW_DEV_SEED_ALERTS", "").strip().lower()
    return v in ("1", "true", "yes", "on")


if _dev_seed_enabled():

    @app.post("/api/dev/seed-demo-alerts")
    async def seed_demo_alerts(
        payload: SeedDemoAlertsRequest = Body(default_factory=SeedDemoAlertsRequest),
    ) -> dict[str, Any]:
        hosts = storage.list_hosts()
        if not hosts:
            raise HTTPException(status_code=404, detail="Нет зарегистрированных хостов")
        hid = payload.host_id
        if hid:
            if not any(h["host_id"] == hid for h in hosts):
                raise HTTPException(status_code=404, detail=f"Хост не найден: {hid}")
        else:
            hid = str(hosts[0]["host_id"])
        now = datetime.now(timezone.utc)
        seeds = [
            ("cpu_percent", 85.0, 87.5, "warning"),
            ("memory_percent", 90.0, 93.0, "warning"),
            ("disk_percent", 90.0, 92.0, "warning"),
        ]
        created: list[dict[str, Any]] = []
        for metric, threshold, value, severity in seeds:
            aid = storage.create_alert(hid, metric, threshold, value, severity, now)
            created.append(
                {"id": aid, "host_id": hid, "metric": metric, "threshold": threshold, "value": value, "severity": severity}
            )
        return {"status": "ok", "host_id": hid, "alerts": created}
