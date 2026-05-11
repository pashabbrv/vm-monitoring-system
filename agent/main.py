from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from agent.collectors import collect_logs, collect_metadata, collect_metrics, collect_processes, collect_traces

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _resolve_config_path() -> Path:
    raw = (os.getenv("AGENT_CONFIG_PATH") or "").strip()
    if raw:
        return Path(raw)
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", "")) / "agent" / "config.yaml"
        if bundled.is_file():
            return bundled
    return Path("agent/config.yaml")


CONFIG_PATH = _resolve_config_path()


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg["server_url"] = os.getenv("SERVER_URL", cfg.get("server_url", "http://localhost:8000"))
    cfg["host_id"] = os.getenv("HOST_ID", cfg.get("host_id", "host-01"))
    cfg["host_name"] = os.getenv("HOST_NAME", cfg.get("host_name", cfg["host_id"]))
    cfg["interval_seconds"] = int(os.getenv("INTERVAL_SECONDS", cfg.get("interval_seconds", 5)))
    cfg["log_paths"] = cfg.get("log_paths", [])
    cfg["process_top_n"] = int(cfg.get("process_top_n", 20))
    cfg["log_lines_limit"] = int(cfg.get("log_lines_limit", 20))
    cfg["enabled"] = True
    return cfg


async def register_host(client: httpx.AsyncClient, config: dict[str, Any]) -> None:
    payload = {
        "host_id": config["host_id"],
        "host_name": config["host_name"],
        "metadata": collect_metadata(),
    }
    response = await client.post("/api/hosts/register", json=payload)
    response.raise_for_status()
    logger.info("Host registered: %s", config["host_id"])


async def push_ingest(client: httpx.AsyncClient, config: dict[str, Any]) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    process_top_n = int(config.get("process_top_n", 20))
    processes = collect_processes(top_n=process_top_n)
    log_paths = []
    for item in config.get("log_paths", []):
        new_item = dict(item)
        new_item["lines"] = min(int(item.get("lines", 20)), int(config.get("log_lines_limit", 20)))
        log_paths.append(new_item)
    payload = {
        "host_id": config["host_id"],
        "ts": ts,
        "metrics": collect_metrics(),
        "processes": processes,
        "logs": collect_logs(log_paths),
        "traces": collect_traces(processes),
    }
    response = await client.post("/ingest", json=payload)
    response.raise_for_status()
    logger.info("Ingest sent for host %s", config["host_id"])


async def pull_remote_config(client: httpx.AsyncClient, config: dict[str, Any]) -> dict[str, Any]:
    response = await client.get(f"/api/agents/config/{config['host_id']}")
    response.raise_for_status()
    remote = response.json()
    config["interval_seconds"] = int(remote.get("interval_seconds", config["interval_seconds"]))
    config["process_top_n"] = int(remote.get("process_top_n", config.get("process_top_n", 20)))
    config["log_lines_limit"] = int(remote.get("log_lines_limit", config.get("log_lines_limit", 20)))
    config["enabled"] = bool(remote.get("enabled", True))
    return config


async def run_agent(stop_event: asyncio.Event) -> None:
    config = load_config()

    def _stop_handler(*_: Any) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop_handler)
        except ValueError:
            pass

    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(base_url=config["server_url"], timeout=timeout) as client:
        backoff = 1
        while not stop_event.is_set():
            try:
                await register_host(client, config)
                break
            except Exception as exc:
                logger.error("Register failed: %s. Retry in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

        while not stop_event.is_set():
            backoff = 1
            while not stop_event.is_set():
                try:
                    config = await pull_remote_config(client, config)
                    if not config.get("enabled", True):
                        await asyncio.sleep(config["interval_seconds"])
                        break
                    await push_ingest(client, config)
                    break
                except Exception as exc:
                    logger.error("Ingest failed: %s. Retry in %ss", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
            await asyncio.sleep(config["interval_seconds"])

    logger.info("Agent stopped gracefully")


def main() -> None:
    asyncio.run(run_agent(asyncio.Event()))


if __name__ == "__main__":
    main()
