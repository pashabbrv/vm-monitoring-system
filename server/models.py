from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


MetricName = Literal[
    "cpu_percent",
    "memory_percent",
    "disk_percent",
    "net_sent_mb",
    "net_recv_mb",
]


class HostMetadata(BaseModel):
    os: str
    cpu_count: int
    total_memory_gb: float


class HostRegistrationRequest(BaseModel):
    host_id: str
    host_name: str
    metadata: HostMetadata


class ProcessEntry(BaseModel):
    pid: int
    name: str
    cpu_percent: float = 0.0
    memory_mb: float = 0.0


class LogEntry(BaseModel):
    service: str | None = None
    level: Literal["INFO", "WARNING", "ERROR"] = "INFO"
    message: str
    ts: datetime


class TraceEntry(BaseModel):
    trace_id: str
    span_id: str
    operation: str
    status: Literal["ok", "error"] = "ok"
    duration_ms: float
    ts: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)


class IngestMetrics(BaseModel):
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    net_sent_mb: float
    net_recv_mb: float


class IngestRequest(BaseModel):
    host_id: str
    ts: datetime
    metrics: IngestMetrics
    processes: list[ProcessEntry] = Field(default_factory=list)
    logs: list[LogEntry] = Field(default_factory=list)
    traces: list[TraceEntry] = Field(default_factory=list)


class HostResponse(BaseModel):
    host_id: str
    name: str
    last_seen: datetime | None = None
    status: Literal["online", "stale"]
    metadata: dict[str, Any] = Field(default_factory=dict)


class MetricPoint(BaseModel):
    ts: datetime
    value: float


class MetricsResponse(BaseModel):
    host_id: str
    metric: MetricName
    points: list[MetricPoint]


class ProcessResponse(BaseModel):
    host_id: str
    ts: datetime | None = None
    processes: list[ProcessEntry]


class LogResponse(BaseModel):
    host_id: str
    logs: list[LogEntry]


class AlertRecord(BaseModel):
    id: int
    host_id: str
    metric: str
    threshold: float
    value: float
    severity: Literal["warning", "critical"]
    started_at: datetime
    resolved_at: datetime | None = None


class SeedDemoAlertsRequest(BaseModel):
    host_id: str | None = None


class HealthResponse(BaseModel):
    status: str


class AgentConfigResponse(BaseModel):
    host_id: str
    interval_seconds: int
    process_top_n: int
    log_lines_limit: int
    enabled: bool
    updated_at: datetime
