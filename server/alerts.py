from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from server import storage


@dataclass(frozen=True)
class AlertRule:
    metric: str
    threshold: float
    duration_seconds: int
    severity: str


DEFAULT_RULES: list[AlertRule] = [
    AlertRule(metric="cpu_percent", threshold=85, duration_seconds=60, severity="warning"),
    AlertRule(metric="cpu_percent", threshold=95, duration_seconds=30, severity="critical"),
    AlertRule(metric="memory_percent", threshold=90, duration_seconds=60, severity="warning"),
    AlertRule(metric="disk_percent", threshold=90, duration_seconds=1, severity="warning"),
    AlertRule(metric="disk_percent", threshold=95, duration_seconds=1, severity="critical"),
]

_breach_state: dict[tuple[str, str, float, str], datetime] = {}


def evaluate_thresholds(host_id: str, ts: datetime, metrics: dict[str, float]) -> None:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    for rule in DEFAULT_RULES:
        value = float(metrics.get(rule.metric, 0.0))
        key = (host_id, rule.metric, rule.threshold, rule.severity)
        active_alert = storage.get_active_alert(host_id, rule.metric, rule.threshold, rule.severity)
        if value > rule.threshold:
            started = _breach_state.get(key)
            if started is None:
                _breach_state[key] = ts
                started = ts
            elapsed = ts - started
            if elapsed >= timedelta(seconds=rule.duration_seconds):
                if active_alert is None:
                    storage.create_alert(host_id, rule.metric, rule.threshold, value, rule.severity, started)
                else:
                    storage.update_alert_value(int(active_alert["id"]), value)
        else:
            _breach_state.pop(key, None)
            if active_alert is not None:
                storage.resolve_alerts(host_id, rule.metric, rule.threshold, rule.severity, ts)
