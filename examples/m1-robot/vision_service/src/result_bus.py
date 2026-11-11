import copy
import threading
import time
from collections import deque


class ResultBus:
    """Thread-safe latest-result bus with bounded history and no frame buffering."""

    def __init__(self, history_size=100):
        self.history = deque(maxlen=max(1, int(history_size)))
        self.condition = threading.Condition()
        self.latest = None
        self.sequence = 0
        self.published = 0
        self.clients = 0

    def publish(self, event):
        item = copy.deepcopy(event)
        with self.condition:
            self.sequence += 1
            item["bus_sequence"] = self.sequence
            self.latest = item
            self.history.append(item)
            self.published += 1
            self.condition.notify_all()
            return copy.deepcopy(item)

    def latest_result(self):
        with self.condition:
            return copy.deepcopy(self.latest)

    def recent(self, limit=20):
        limit = max(1, min(int(limit), self.history.maxlen))
        with self.condition:
            return copy.deepcopy(list(self.history)[-limit:])

    def wait_for_update(self, after_sequence, timeout=5.0):
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self.condition:
            while self.sequence <= after_sequence:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(remaining)
            return copy.deepcopy(self.latest)

    def client_opened(self):
        with self.condition:
            self.clients += 1

    def client_closed(self):
        with self.condition:
            self.clients = max(0, self.clients - 1)

    def status(self):
        with self.condition:
            latest = self.latest
            age = None
            if latest and latest.get("received_monotonic") is not None:
                age = max(0.0, time.monotonic() - latest["received_monotonic"])
            return {
                "published": self.published,
                "history_size": len(self.history),
                "history_capacity": self.history.maxlen,
                "clients": self.clients,
                "latest_sequence": self.sequence,
                "latest_age_seconds": round(age, 3) if age is not None else None,
            }
