from __future__ import annotations

import os
import platform
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil


def _logical_cpu_count() -> int:
    return max(1, int(psutil.cpu_count(logical=True) or 1))


def _normalize_proc_cpu(cpu_value: float) -> float:
    normalized = float(cpu_value) / float(_logical_cpu_count())
    return max(0.0, min(100.0, normalized))


def _is_idle_process(pid: int, name: str) -> bool:
    n = name.strip().lower()
    return pid == 0 or n in {"system idle process", "idle", "idle process"}


def _synthetic_traces_enabled() -> bool:
    v = os.getenv("ENABLE_SYNTHETIC_TRACES", "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _disk_usage_path() -> str:
    configured = os.getenv("DISK_USAGE_PATH", "").strip()
    if configured:
        return configured
    if os.name == "nt":
        drive = os.getenv("SystemDrive", "C:")
        return f"{drive}\\"
    return "/"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def collect_metrics() -> dict[str, float]:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(_disk_usage_path())
    net = psutil.net_io_counters()
    return {
        "cpu_percent": float(psutil.cpu_percent(interval=0.2)),
        "memory_percent": float(vm.percent),
        "disk_percent": float(disk.percent),
        "net_sent_mb": float(net.bytes_sent / 1024 / 1024),
        "net_recv_mb": float(net.bytes_recv / 1024 / 1024),
    }


def collect_processes(top_n: int = 20) -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for proc in psutil.process_iter(attrs=["pid", "name", "cpu_percent", "memory_info"]):
        try:
            info = proc.info
            memory_mb = float((info["memory_info"].rss if info.get("memory_info") else 0.0) / 1024 / 1024)
            processes.append(
                {
                    "pid": int(info["pid"]),
                    "name": str(info.get("name") or "unknown"),
                    "cpu_percent": _normalize_proc_cpu(float(info.get("cpu_percent") or 0.0)),
                    "memory_mb": memory_mb,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    processes.sort(key=lambda x: (x["cpu_percent"], x["memory_mb"]), reverse=True)
    return processes[:top_n]


def _detect_level(message: str) -> str:
    msg = message.upper()
    if "ERROR" in msg or "FATAL" in msg:
        return "ERROR"
    if "WARN" in msg:
        return "WARNING"
    return "INFO"


def collect_logs(log_paths: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not log_paths:
        return []
    rows: list[dict[str, Any]] = []
    for item in log_paths:
        service = str(item.get("service", "service"))
        path = Path(str(item.get("path", "")))
        lines_count = int(item.get("lines", 20))
        if not path.exists() or not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                tail = list(deque(f, maxlen=lines_count))
            for line in tail:
                text = line.strip()
                if not text:
                    continue
                rows.append(
                    {
                        "service": service,
                        "level": _detect_level(text),
                        "message": text,
                        "ts": _now().isoformat(),
                    }
                )
        except OSError:
            continue
    return rows


def collect_metadata() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    return {
        "os": f"{platform.system()} {platform.release()}",
        "cpu_count": int(psutil.cpu_count(logical=True) or 1),
        "total_memory_gb": round(float(vm.total / 1024 / 1024 / 1024), 2),
    }


def collect_traces(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not _synthetic_traces_enabled():
        return []
    rows: list[dict[str, Any]] = []
    now = _now().isoformat()
    for proc in processes[:5]:
        pid = int(proc.get("pid", 0))
        name = str(proc.get("name", "unknown"))
        if _is_idle_process(pid, name):
            continue
        cpu = float(proc.get("cpu_percent", 0.0))
        if cpu < 60:
            continue
        trace_id = uuid.uuid4().hex
        rows.append(
            {
                "trace_id": trace_id,
                "span_id": trace_id[:16],
                "operation": f"process.{name}.cpu_sample",
                "status": "ok",
                "duration_ms": round(cpu * 10, 2),
                "ts": now,
                "attributes": {
                    "pid": pid,
                    "cpu_percent": cpu,
                    "memory_mb": float(proc.get("memory_mb", 0.0)),
                    "synthetic": True,
                },
            }
        )
    return rows
