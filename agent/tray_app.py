from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

import pystray
from PIL import Image

from agent.main import run_agent


def _log_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MonitoringAgent"
    base.mkdir(parents=True, exist_ok=True)
    return base / "agent.log"


def _setup_logging() -> None:
    log_file = _log_path()
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root.addHandler(fh)


def _icon_image() -> Image.Image:
    img = Image.new("RGB", (64, 64), (52, 120, 200))
    for x in range(18, 46):
        for y in range(18, 46):
            img.putpixel((x, y), (255, 255, 255))
    return img


def main() -> None:
    _setup_logging()
    stop_event = asyncio.Event()

    def _run_agent() -> None:
        asyncio.run(run_agent(stop_event))

    threading.Thread(target=_run_agent, daemon=True).start()

    def on_quit(icon: pystray.Icon, _item: object) -> None:
        stop_event.set()
        icon.stop()

    icon = pystray.Icon(
        "monitoring_agent",
        _icon_image(),
        "Мониторинг хоста",
        menu=pystray.Menu(pystray.MenuItem("Выход", on_quit)),
    )
    icon.run()
