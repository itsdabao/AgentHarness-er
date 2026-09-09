"""Process-lifetime advisory lock; OS releases it if the process crashes."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import BinaryIO


class DatabaseOwnership:
    def __init__(self, path: Path) -> None:
        self.path = path.with_name(path.name + ".owner")
        self.stream: BinaryIO | None = None

    def acquire(self) -> None:
        stream = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise ValueError("Database is already owned by another runtime") from exc
        self.stream = stream

    def release(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None
