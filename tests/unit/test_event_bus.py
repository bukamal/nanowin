"""The domain event bus: pub/sub, unsubscription, isolation."""

from __future__ import annotations

from nano_offline.core.event_bus import MUTATION_EVENTS, EventBus


def test_subscribe_publish_unsubscribe():
    bus = EventBus()
    got = []
    unsubscribe = bus.subscribe("thing.created", lambda event, payload: got.append((event, payload)))
    bus.publish("thing.created", {"id": 1})
    assert got == [("thing.created", {"id": 1})]
    unsubscribe()
    bus.publish("thing.created", {"id": 2})
    assert len(got) == 1


def test_a_failing_listener_does_not_stop_others():
    bus = EventBus()
    got = []
    bus.subscribe("x", lambda e, p: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.subscribe("x", lambda e, p: got.append("ok"))
    bus.publish("x")
    assert got == ["ok"]


def test_mutation_events_cover_the_domains():
    for name in (
        "invoice.created", "invoice.updated", "invoice.deleted",
        "item.created", "voucher.created", "expense.created", "party.created",
    ):
        assert name in MUTATION_EVENTS


class _FakePage:
    def __init__(self):
        self._nano_data_version = 0

    def run_task(self, handler, *args, **kwargs):
        import asyncio

        result = handler(*args, **kwargs)
        if hasattr(result, "__await__"):
            try:
                asyncio.run(result)
            except Exception:
                pass


def test_attach_page_invalidation_bumps_version_and_refreshes_badge():
    from nano_offline.core import event_bus

    bus = event_bus.bus()
    bus.clear()  # isolate from any other subscribers
    try:
        page = _FakePage()
        refreshes = []
        page._nano_notification_refresh = lambda: refreshes.append(True)
        event_bus.attach_page_invalidation(page)

        event_bus.publish(event_bus.INVOICE_CREATED, {"id": 1})
        assert page._nano_data_version == 1
        assert refreshes == [True]

        # Idempotent: a second attach must not double-subscribe.
        event_bus.attach_page_invalidation(page)
        event_bus.publish(event_bus.VOUCHER_DELETED, {"id": 2})
        assert page._nano_data_version == 2
        assert refreshes == [True, True]
    finally:
        bus.clear()
