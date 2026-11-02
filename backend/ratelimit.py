"""Per-user rate limiting (B1) - a simple sliding window kept in memory.
Production: the same idea in Redis so all server instances share the counts."""
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_requests: int = 20, window_seconds: int = 60):
        self.max_requests, self.window = max_requests, window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, user_id: str) -> bool:
        now = time.time()
        q = self._hits[user_id]
        while q and now - q[0] > self.window:   # forget requests older than the window
            q.popleft()
        if len(q) >= self.max_requests:
            return False
        q.append(now)
        return True
