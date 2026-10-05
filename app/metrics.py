"""Bounded operational measurements; never include raw upstream messages."""
from collections import Counter, deque
import json
import logging
from typing import Any


def event(logger: logging.Logger, level: int, stage: str, symbol: str | None = None,
          error: BaseException | None = None, **fields: Any) -> None:
    logger.log(level, json.dumps(dict(stage=stage, symbol=symbol,
               error_type=type(error).__name__ if error else None, **fields), ensure_ascii=False))


class Metrics:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.latencies: deque[float] = deque(maxlen=1000)
        self.scan_seconds: dict[str, float] = {}

    def status(self) -> dict[str, Any]:
        values = sorted(self.latencies)
        return dict(counts=dict(self.counts), scan_seconds=dict(self.scan_seconds),
                    latency_samples=len(values), symbol_p50_ms=values[len(values)//2] if values else 0,
                    symbol_p95_ms=values[min(len(values)-1, int(len(values)*.95))] if values else 0)
