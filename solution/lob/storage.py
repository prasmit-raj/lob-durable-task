"""Append-only command log.

One line per command, holding every event that command produced, so a command
is on disk either whole or not at all. Each line is `<crc32 hex> <json>\\n`.
A torn or corrupt tail (from a kill mid-write) is cut off on startup.

A process kill does not lose data already handed to the kernel, so a plain
write() before acknowledging is enough; set LOB_FSYNC=1 to also survive power loss.
"""
from __future__ import annotations

import json
import os
import zlib


class Log:
    def __init__(self, directory: str) -> None:
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, "events.log")
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        self.fsync = os.environ.get("LOB_FSYNC") == "1"

    def load(self) -> list[list[dict]]:
        records, good = [], 0
        with open(self.path, "rb") as f:
            for line in f:
                record = self._decode(line)
                if record is None:
                    break
                records.append(record)
                good += len(line)
        # Truncating is a single syscall, so a kill here leaves either the old or new length.
        if good != os.fstat(self.fd).st_size:
            os.ftruncate(self.fd, good)
        return records

    def append(self, events: list[dict]) -> None:
        body = json.dumps(events, separators=(",", ":")).encode()
        data = b"%08x %s\n" % (zlib.crc32(body), body)
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view):]
        if self.fsync:
            os.fsync(self.fd)

    @staticmethod
    def _decode(line: bytes) -> list[dict] | None:
        if not line.endswith(b"\n") or len(line) < 10 or line[8:9] != b" ":
            return None
        body = line[9:-1]
        try:
            if int(line[:8], 16) != zlib.crc32(body):
                return None
            return json.loads(body)
        except ValueError:
            return None
