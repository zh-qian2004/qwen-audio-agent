"""Append-only JSONL audit trail for motion commands.

The Dashboard was previously read-only; every command now leaves a durable
record (who/when/what/why plus vendor response and ROS observation) so an
operator can reconstruct an incident after the fact.
"""

import json
import logging
import os
import threading
import time


LOG = logging.getLogger("dashboard.audit")


class ControlAudit:
    def __init__(self, path, max_bytes=4 * 1024 * 1024, backups=3):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, record):
        record = dict(record)
        record.setdefault("logged_at", time.time())
        record.setdefault("logged_at_iso", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()))
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                self._rotate_if_needed(len(line) + 1)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
            except OSError as error:
                LOG.error("cannot write audit record: %s", error)

    def tail(self, limit=50):
        limit = max(1, min(int(limit), 500))
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                    lines = handle.readlines()[-limit:]
            except FileNotFoundError:
                return []
            except OSError as error:
                LOG.error("cannot read audit records: %s", error)
                return []
        records = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def _rotate_if_needed(self, incoming):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size + incoming <= self.max_bytes:
            return
        for index in range(self.backups, 0, -1):
            source = self.path if index == 1 else f"{self.path}.{index - 1}"
            target = f"{self.path}.{index}"
            if os.path.exists(source):
                try:
                    os.replace(source, target)
                except OSError:
                    pass
