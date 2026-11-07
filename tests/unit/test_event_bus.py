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
