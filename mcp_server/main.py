from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from mcp_server import tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP("monitoring-mcp", host="0.0.0.0", port=8765)


@mcp.tool()
async def list_hosts() -> list[dict]:
    """Hosts registered with the monitoring server (host_id, name, online/stale).
    Use first when the user names a host vaguely ("контейнер 1") to resolve host_id.
    Do not use for logs, traces, or alerts."""
    return await tools.list_hosts_impl()


@mcp.tool()
async def get_metrics(host_id: str, metric: str, since: str = "5m", until: str = "now") -> dict:
    """Numeric time series: cpu_percent, memory_percent, disk_percent, net_sent_mb, net_recv_mb.
    Use for graphs, trends, "какой CPU за период". Not for trace_id and not for log lines."""
    return await tools.get_metrics_impl(host_id=host_id, metric=metric, since=since, until=until)


@mcp.tool()
async def get_top_processes(host_id: str, by: str = "cpu", n: int = 10) -> list[dict]:
    """Latest snapshot: top processes by CPU or memory from the last ingest.
    Not logs, not traces table, not alerts."""
    return await tools.get_top_processes_impl(host_id=host_id, by=by, n=n)


@mcp.tool()
async def get_recent_logs(
    host_id: str,
    service: str | None = None,
    lines: int = 100,
    level: str | None = None,
) -> list[dict]:
    """Tail lines from log files that the host agent is configured to read (paths in agent config).
    Each row is a text log line with level INFO/WARNING/ERROR inferred from content.
    Use for "покажи ошибки в логах", "warnings", service log tail. Never use this to
    find a trace_id: trace IDs live in the traces store, not in log tail text."""
    return await tools.get_recent_logs_impl(host_id=host_id, service=service, lines=lines, level=level)


@mcp.tool()
async def get_active_alerts(host_id: str | None = None) -> list[dict]:
    """Threshold-based alerts from the monitoring DB (CPU/memory/disk rules).
    Use for "активные алерты", "что горит". Not the same as log ERROR lines."""
    return await tools.get_active_alerts_impl(host_id=host_id)


@mcp.tool()
async def compare_hosts(host_ids: list[str], metric: str, since: str = "15m") -> dict:
    """Compare one numeric metric across several host_ids over a window. Not for traces or logs."""
    return await tools.compare_hosts_impl(host_ids=host_ids, metric=metric, since=since)


@mcp.tool()
async def diagnose_host(host_id: str) -> dict:
    """One-shot bundle: current metrics, top processes, active_alerts, last ERROR log lines,
    and recent_traces (last 20 from DB). Use for broad "что не так с хостом". For only
    traces list use get_recent_traces; for a pasted trace_id use get_trace_by_id."""
    return await tools.diagnose_host_impl(host_id=host_id)


@mcp.tool()
async def get_recent_traces(host_id: str, limit: int = 50) -> list[dict]:
    """Rows from the monitoring traces table (same source as the dashboard "Последние трассировки"):
    trace_id, span_id, operation, status, duration_ms, ts, attributes. Use for "последние
    трассировки", "список трейсов", overview. Not log files; not searchable by grepping logs."""
    return await tools.get_recent_traces_impl(host_id=host_id, limit=limit)


@mcp.tool()
async def get_trace_by_id(host_id: str, trace_id: str) -> list[dict]:
    """Exact lookup in the traces table by trace_id for one host_id. Use when the user
    pastes a hex trace_id from the UI. Do not use get_recent_logs for this."""
    return await tools.get_trace_by_id_impl(host_id=host_id, trace_id=trace_id)


def main() -> None:
    logger.info("MCP server listening on :8765, 9 tools registered")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
