import threading
import time


class ProcessorError(RuntimeError):
    pass


class BaseProcessor:
    STATES = {"stopped", "starting", "running", "degraded", "failed"}

    def __init__(self, processor_id, config):
        self.processor_id = processor_id
        self.config = dict(config)
        self.lock = threading.RLock()
        self.state = "stopped"
        self.started_at = None
        self.stopped_at = None
        self.last_error = None

    def _set_state(self, state, error=None):
        if state not in self.STATES:
            raise ValueError(f"invalid processor state: {state}")
        with self.lock:
            self.state = state
            self.last_error = error
            if state == "starting":
                self.started_at = time.time()
            if state == "stopped":
                self.stopped_at = time.time()

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def status(self):
        with self.lock:
            return {
                "id": self.processor_id,
                "state": self.state,
                "started_at": self.started_at,
                "stopped_at": self.stopped_at,
                "last_error": self.last_error,
            }
