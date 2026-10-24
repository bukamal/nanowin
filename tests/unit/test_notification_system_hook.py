from __future__ import annotations

from datetime import datetime

from nano_offline.core import platform_capabilities
from nano_offline.services.notification_service import Alert, NotificationService


class _Cursor:
    rowcount = 1


class _Connection:
    def execute(self, *args, **kwargs):
        return _Cursor()


class _Transaction:
    def __enter__(self):
        return _Connection()

    def __exit__(self, *exc):
        return False


class _Database:
    def transaction(self):
        return _Transaction()


def _alert() -> Alert:
    return Alert(
        rule_key="low_stock",
        dedupe_key="low_stock:2026-10-25",
        severity="warning",
        title="مخزون منخفض",
        body="صنف واحد وصل إلى الحد الأدنى",
    )


def _service(alerts: list[Alert], config: dict) -> NotificationService:
    service = object.__new__(NotificationService)
    service.db = _Database()
    service._external_hook = None
    service._system_hook = None
    service.generate_alerts = lambda: alerts
    service.get_config = lambda: config
    return service


def test_sync_sends_fresh_alerts_to_system_hook():
    calls: list[list[Alert]] = []
    service = _service([_alert()], {"quiet_hours": {"enabled": False}})
    service.set_system_hook(calls.append)

    service.sync()

    assert len(calls) == 1
    assert calls[0][0].rule_key == "low_stock"


def test_sync_skips_system_hook_during_quiet_hours():
    hour = datetime.now().hour
    calls: list[list[Alert]] = []
    service = _service(
        [_alert()],
        {"quiet_hours": {"enabled": True, "start_hour": hour, "end_hour": (hour + 1) % 24}},
    )
    service.set_system_hook(calls.append)

    service.sync()

    assert calls == []


def test_quiet_hours_overnight_window():
    cfg = {"quiet_hours": {"enabled": True, "start_hour": 22, "end_hour": 8}}

    assert NotificationService._in_quiet_hours(cfg, datetime(2026, 1, 1, 23, 0))
    assert NotificationService._in_quiet_hours(cfg, datetime(2026, 1, 1, 2, 0))
    assert not NotificationService._in_quiet_hours(cfg, datetime(2026, 1, 1, 12, 0))


def test_windows_notification_capabilities(monkeypatch):
    monkeypatch.setattr(platform_capabilities, "IS_WINDOWS", True)

    assert platform_capabilities.feature_status("notifications") == "native"
    assert platform_capabilities.feature_status("background_notifications") == "off:app-open-only"


def test_android_notification_capabilities(monkeypatch):
    monkeypatch.setattr(platform_capabilities, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_capabilities, "IS_ANDROID", True)

    assert platform_capabilities.feature_status("notifications") == "native"
    assert platform_capabilities.feature_status("background_notifications") == "native"
