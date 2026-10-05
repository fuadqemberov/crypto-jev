"""Bounded LRU; persisted answers are still TTL- and schema-validated on every hit."""
from collections import OrderedDict
import time
from typing import Any
from .jev import parse_response, QUESTIONS, POSITION_QUESTIONS


class AnswerCache:
    def __init__(self, capacity: int, ttl: int, store: Any = None) -> None:
        self.capacity, self.ttl, self.store = capacity, ttl, store
        self.items: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self.hits = self.misses = self.evictions = 0

    def get(self, key: str, position: bool = False) -> dict[str, Any] | None:
        value = self.items.get(key)
        if value is not None and self.store:
            self.store.cache_touch(key)
        if value is None and self.store:
            value = self.store.cache_get(key)
        if value is not None:
            created, answer = value
            if 0 <= time.time() - created < self.ttl:
                questions = {**QUESTIONS, **POSITION_QUESTIONS} if position else QUESTIONS
                parsed = parse_response(answer, questions)
                self._put(key, (created, parsed))
                self.hits += 1
                return parsed
            self.items.pop(key, None)
        self.misses += 1
        return None

    def _put(self, key: str, value: tuple[float, dict[str, Any]]) -> None:
        self.items[key] = value
        self.items.move_to_end(key)
        while len(self.items) > self.capacity:
            self.items.popitem(last=False)
            self.evictions += 1

    def put(self, key: str, answer: dict[str, Any]) -> None:
        value = (time.time(), answer)
        if self.store:
            self.store.cache_put(key, value, self.capacity, self.ttl)
        self._put(key, value)

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return dict(size=len(self.items), hits=self.hits, misses=self.misses, evictions=self.evictions,
                    hit_rate=self.hits / total if total else 0., persistent=self.store is not None)
