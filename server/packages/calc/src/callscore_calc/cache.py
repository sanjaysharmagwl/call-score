"""A byte-bounded LRU cache of result frames.

Keys are content hashes of everything that determines a result (dataset version, transform steps,
canonical query, options, library versions), so entries never go stale and never need
invalidating; old ones simply fall out.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Generic, TypeVar

import polars as pl

T = TypeVar("T")


class ResultCache(Generic[T]):
    def __init__(self, max_bytes: int = 256 * 1024 * 1024) -> None:
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, tuple[T, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> T | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return item[0]

    def put(self, key: str, value: T, size: int) -> None:
        if size > self.max_bytes:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            self._items[key] = (value, size)
            self._bytes += size
            while self._bytes > self.max_bytes and self._items:
                _, (_, evicted) = self._items.popitem(last=False)
                self._bytes -= evicted

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    @property
    def size_bytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._items)


def frame_size(frame: pl.DataFrame) -> int:
    return int(frame.estimated_size()) + 256
