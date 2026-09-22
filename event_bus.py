import asyncio
import json
from collections import defaultdict


class EventBus:
    """In-memory pub/sub for SSE. Each widget_id maps to a set of subscriber queues.
    When a new submission arrives, publish() fans it out to all connected clients."""

    def __init__(self):
        self._subscribers: dict[int, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, widget_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers[widget_id].append(q)
        return q

    def unsubscribe(self, widget_id: int, q: asyncio.Queue):
        subs = self._subscribers.get(widget_id, [])
        if q in subs:
            subs.remove(q)
        if not subs and widget_id in self._subscribers:
            del self._subscribers[widget_id]

    def publish(self, widget_id: int, event: dict):
        data = json.dumps(event)
        for q in self._subscribers.get(widget_id, []):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass


bus = EventBus()
