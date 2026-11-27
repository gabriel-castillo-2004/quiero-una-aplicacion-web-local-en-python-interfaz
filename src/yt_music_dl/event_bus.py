"""Thread-safe in-memory event bus with per-job snapshot and SSE streaming."""

from __future__ import annotations

from collections import deque
from typing import Any, Iterator
import copy
import json
import queue
import threading

from yt_music_dl.models import TERMINAL_STATUSES


class InMemoryEventBus:
    """Thread-safe EventBusPort implementation with per-job history and SSE support."""

    def __init__(self, history_limit: int = 100) -> None:
        self._history_limit = history_limit
        self._lock = threading.Lock()
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._subscribers: dict[str, list[queue.Queue[dict[str, Any]]]] = {}

    def publish(self, job_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Store latest snapshot/history and notify all subscribers for job_id."""
        safe_payload = copy.deepcopy(payload)
        event = {"event": event_type, "data": safe_payload}
        with self._lock:
            self._snapshots[job_id] = copy.deepcopy(safe_payload)
            history = self._history.setdefault(
                job_id, deque(maxlen=self._history_limit)
            )
            history.append(event)
            for sub in list(self._subscribers.get(job_id, [])):
                sub.put(copy.deepcopy(event))

    def subscribe_with_snapshot(
        self, job_id: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], queue.Queue[dict[str, Any]]]:
        """Atomically capture latest snapshot, recent events, and register a live queue."""
        q: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            snapshot = (
                dict(self._snapshots[job_id]) if job_id in self._snapshots else None
            )
            recent = list(self._history.get(job_id, ()))
            self._subscribers.setdefault(job_id, []).append(q)
        return snapshot, recent, q

    def unsubscribe(self, job_id: str, q: queue.Queue[dict[str, Any]]) -> None:
        """Remove a subscriber queue for job_id."""
        with self._lock:
            subs = self._subscribers.get(job_id)
            if subs and q in subs:
                subs.remove(q)

    def stream_sse(
        self,
        job_id: str,
        *,
        initial_snapshot: dict[str, Any] | None = None,
        heartbeat_interval: float = 15.0,
    ) -> Iterator[str]:
        """Yield SSE frames for job_id starting with snapshot and 15s heartbeats."""
        snapshot, recent, q = self.subscribe_with_snapshot(job_id)
        try:
            effective_snapshot = snapshot if snapshot is not None else initial_snapshot
            if effective_snapshot is not None:
                yield self._format_sse("snapshot", effective_snapshot)
                if effective_snapshot.get("status") in TERMINAL_STATUSES:
                    return

            for item in recent:
                if item["event"] in TERMINAL_STATUSES:
                    yield self._format_sse(item["event"], item["data"])
                    return

            while True:
                try:
                    msg = q.get(timeout=heartbeat_interval)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue

                yield self._format_sse(msg["event"], msg["data"])
                if msg["event"] in TERMINAL_STATUSES:
                    break
        finally:
            self.unsubscribe(job_id, q)

    @staticmethod
    def _format_sse(event_name: str, data: dict[str, Any]) -> str:
        return f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
