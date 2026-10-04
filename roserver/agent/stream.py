"""PublicEventBuffer: roserver-owned Product sequence, replay and fan-out.

Docs §3.13 / §3.14.  One buffer per root run; Product sequence is allocated
here, never reused from Runtime, and state-transition events are never pruned.
"""

from __future__ import annotations

import time
from typing import Any

PROTECTED_EVENT_TYPES = frozenset(
    {
        "run.started",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.interrupted",
        "child_run.started",
        "child_run.completed",
        "child_run.failed",
        "child_run.cancelled",
        "tool.effect_committed",
        "approval.requested",
        "approval.resolved",
        "stream.resync_required",
    }
)


class Subscription:
    """A live fan-out subscription; never used across event loops."""

    def __init__(self, maxsize: int) -> None:
        import asyncio

        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize)
        self.lost = False
        self.closed = False

    def push(self, event: dict[str, Any]) -> None:
        if self.closed:
            return
        if self.queue.full():
            # Slow consumer: drop the queue and require an explicit resync.
            self.lost = True
            while not self.queue.empty():
                self.queue.get_nowait()
        self.queue.put_nowait(event)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)


class PublicEventBuffer:
    """Bounded, replayable Product event log for one root run."""

    def __init__(
        self,
        root_run_id: str,
        session_id: str,
        *,
        max_events: int = 1000,
        max_bytes: int = 4 * 1024 * 1024,
        retention: float = 300.0,
        subscriber_queue_size: int = 256,
    ) -> None:
        self.root_run_id = root_run_id
        self.session_id = session_id
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.retention = retention
        self.subscriber_queue_size = subscriber_queue_size
        self.last_sequence = 0
        self.oldest_available_sequence = 1
        self.terminal = False
        self.terminal_at: float | None = None
        self._events: list[dict[str, Any]] = []
        self._bytes = 0
        self._subscriptions: list[Subscription] = []

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._events)

    def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        self.last_sequence += 1
        event = dict(event)
        event["sequence"] = self.last_sequence
        self._events.append(event)
        self._bytes += len(str(event))
        self._trim()
        event_type = event.get("type")
        if event_type in {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}:
            self.terminal = True
            self.terminal_at = time.time()
        for subscription in tuple(self._subscriptions):
            subscription.push(event)
        return event

    def subscribe(self) -> Subscription:
        subscription = Subscription(self.subscriber_queue_size)
        self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    def replay(self, after_sequence: int) -> tuple[list[dict[str, Any]], bool]:
        """Return ``(events, resync_required)`` for ``sequence > after_sequence``.

        A resync is required whenever any sequence in ``(after, last]`` was
        pruned or never retained: replay must never silently skip a sequence.
        """
        if after_sequence >= self.last_sequence:
            return [], False
        retained = {event["sequence"]: event for event in self._events}
        wanted = range(after_sequence + 1, self.last_sequence + 1)
        if any(sequence not in retained for sequence in wanted):
            return [], True
        return [retained[sequence] for sequence in wanted], False

    def is_expired(self, now: float | None = None) -> bool:
        if not self.terminal or self.terminal_at is None:
            return False
        moment = time.time() if now is None else now
        return moment - self.terminal_at > self.retention

    def resync_event(
        self,
        *,
        requested_after_sequence: int | None,
        sequence: int,
        timestamp: str,
    ) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "root_run_id": self.root_run_id,
            "source_run_id": self.root_run_id,
            "parent_run_id": None,
            "sequence": sequence,
            "type": "stream.resync_required",
            "timestamp": timestamp,
            "data": {
                "requested_after_sequence": requested_after_sequence,
                "oldest_available_sequence": (
                    self.oldest_available_sequence if self._events else None
                ),
                "last_sequence": self.last_sequence,
            },
        }

    def _trim(self) -> None:
        while (
            len(self._events) > self.max_events or self._bytes > self.max_bytes
        ) and len(self._events) > 1:
            index = next(
                (
                    position
                    for position, event in enumerate(self._events)
                    if event.get("type") not in PROTECTED_EVENT_TYPES
                ),
                None,
            )
            if index is None:
                break
            removed = self._events.pop(index)
            self._bytes -= len(str(removed))
        self.oldest_available_sequence = (
            self._events[0]["sequence"] if self._events else self.last_sequence + 1
        )


__all__ = ["PROTECTED_EVENT_TYPES", "PublicEventBuffer", "Subscription"]
