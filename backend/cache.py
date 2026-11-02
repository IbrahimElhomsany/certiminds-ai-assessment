"""Response cache (B4).

What we cache: the structured analysis of a message (same text + task + language + model
+ prompt version => same answer). It saves an LLM call (~1-3 s and real money).

What we NEVER cache: inventory, prices, quotes or anything user-specific - those must
always be fresh from the catalog/tools. We also skip caching messages that look like they
contain personal data (emails, phone numbers), because the cache is shared across users.
"""
import hashlib
import re
import time
from typing import Optional

PII_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s-]{8,}\d")


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def make_key(*, model: str, prompt_version: str, task: str, language: str, message: str) -> str:
    """No user_id in the key on purpose: two users asking the same generic question share
    the answer. Safe only because PII messages are never cached."""
    raw = "|".join([model, prompt_version, task, language, normalize(message)])
    return hashlib.sha256(raw.encode()).hexdigest()


class TTLCache:
    def __init__(self, ttl_seconds: int = 3600, max_items: int = 1000):
        self.ttl, self.max_items = ttl_seconds, max_items
        self._data: dict[str, tuple[float, str]] = {}

    @staticmethod
    def cacheable(message: str) -> bool:
        return not PII_PATTERN.search(message)

    def get(self, key: str) -> Optional[str]:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if time.time() > expires:          # invalidation by time (TTL)
            del self._data[key]
            return None
        return value

    def set(self, key: str, value: str):
        if len(self._data) >= self.max_items:      # crude eviction: drop the oldest entry
            self._data.pop(next(iter(self._data)))
        self._data[key] = (time.time() + self.ttl, value)

    def clear(self):
        """Invalidation on change: called when the prompt or catalog changes."""
        self._data.clear()
