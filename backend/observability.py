"""Metrics and structured logging (B5).

In-memory for the assessment. In production these same numbers would be exported
to Prometheus/OpenTelemetry and shown in Grafana dashboards with alerts.
"""
import json
import logging
import statistics
import threading
from collections import Counter, defaultdict

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("certiminds")


def log_event(event: str, **fields):
    """One JSON object per line -> easy to search. Never pass secrets or full customer messages."""
    logger.info(json.dumps({"event": event, **fields}, default=str))


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        self.requests = 0
        self.errors = Counter()            # by error kind
        self.models = Counter()            # which model served each request
        self.latency_ms = defaultdict(list)  # "llm", "retrieval", "total"
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.cache_hits = 0
        self.cache_misses = 0
        self.fallbacks = 0
        self.retries = 0

    def record_latency(self, kind: str, ms: float):
        with self._lock:
            self.latency_ms[kind].append(ms)

    def record_llm(self, model: str, input_tokens: int, output_tokens: int, cost: float):
        with self._lock:
            self.models[model] += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.cost_usd += cost

    def incr(self, name: str, n: int = 1):
        with self._lock:
            setattr(self, name, getattr(self, name) + n)

    def error(self, kind: str):
        with self._lock:
            self.errors[kind] += 1

    @staticmethod
    def _summary(values):
        if not values:
            return {"count": 0}
        s = sorted(values)
        return {"count": len(s), "avg": round(statistics.mean(s), 1),
                "p50": round(s[len(s) // 2], 1), "p95": round(s[min(len(s) - 1, int(len(s) * 0.95))], 1)}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "request_count": self.requests,
                "errors": dict(self.errors),
                "selected_models": dict(self.models),
                "latency_ms": {k: self._summary(v) for k, v in self.latency_ms.items()},
                "tokens": {"input": self.input_tokens, "output": self.output_tokens},
                "estimated_cost_usd": round(self.cost_usd, 6),
                "cache": {"hits": self.cache_hits, "misses": self.cache_misses},
                "retries": self.retries,
                "fallbacks": self.fallbacks,
            }


METRICS = Metrics()
