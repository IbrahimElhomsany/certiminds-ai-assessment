"""The LLM gateway (B1, B2, B3).

One place that every LLM call goes through. It adds:
- timeout on each call
- retries with exponential backoff + jitter for temporary errors
- fallback to the next provider when one keeps failing
- schema validation of the output (invalid output never reaches the app)
- token, cost, latency and model metrics + structured logs
- caching for safe, repeatable requests
"""
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Type

from pydantic import BaseModel, ValidationError

from backend.cache import TTLCache, make_key
from backend.observability import METRICS, log_event
from backend.providers import BaseProvider, ErrorKind, ProviderError, estimate_cost


class GatewayError(Exception):
    """Raised when every provider and every retry failed."""
    def __init__(self, kind: ErrorKind, attempts: list):
        super().__init__(f"All providers failed (last error: {kind.value})")
        self.kind, self.attempts = kind, attempts


@dataclass
class GatewayResult:
    output: BaseModel
    request_id: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    latency_ms: float = 0.0
    cached: bool = False
    fallback_used: bool = False
    attempts: list = field(default_factory=list)


class LLMGateway:
    def __init__(self, providers: list[BaseProvider], *, max_retries: int = 3,
                 base_delay_s: float = 0.5, max_delay_s: float = 8.0, timeout_s: float = 30.0,
                 cache: Optional[TTLCache] = None, prompt_version: str = "v1",
                 sleep=time.sleep):
        if not providers:
            raise ValueError("At least one provider is required")
        self.providers = providers            # in priority order: primary first, fallbacks after
        self.max_retries = max_retries
        self.base_delay_s, self.max_delay_s = base_delay_s, max_delay_s
        self.timeout_s = timeout_s
        self.cache = cache
        self.prompt_version = prompt_version
        self._sleep = sleep                   # injectable so tests don't really wait

    def backoff_delay(self, attempt: int) -> float:
        """Exponential backoff: 0.5s, 1s, 2s, 4s ... capped, plus random jitter so many
        clients don't all retry at the same instant."""
        delay = min(self.max_delay_s, self.base_delay_s * (2 ** attempt))
        return delay + random.uniform(0, delay * 0.1)

    def generate_structured(self, *, system: str, user: str, output_model: Type[BaseModel],
                            task: str = "generic", language: str = "en",
                            use_cache: bool = True) -> GatewayResult:
        request_id = uuid.uuid4().hex[:12]
        start = time.perf_counter()

        cache_key = None
        if self.cache is not None and use_cache and self.cache.cacheable(user):
            cache_key = make_key(model=self.providers[0].model, prompt_version=self.prompt_version,
                                 task=task, language=language, message=user)
            hit = self.cache.get(cache_key)
            if hit is not None:
                METRICS.incr("cache_hits")
                log_event("llm_cache_hit", request_id=request_id, task=task)
                return GatewayResult(output=output_model.model_validate_json(hit), request_id=request_id,
                                     model=self.providers[0].model, provider="cache", cached=True,
                                     latency_ms=round((time.perf_counter() - start) * 1000, 1))
            METRICS.incr("cache_misses")

        attempts, last_kind = [], ErrorKind.SERVER_ERROR
        for p_index, provider in enumerate(self.providers):
            if p_index > 0:
                METRICS.incr("fallbacks")
                log_event("llm_fallback", request_id=request_id, to_provider=provider.name)
            for attempt in range(self.max_retries + 1):
                t0 = time.perf_counter()
                try:
                    resp = provider.generate(system, user, output_model, self.timeout_s)
                    try:                                   # B2: never trust the raw text
                        parsed = output_model.model_validate_json(resp.text)
                    except ValidationError as e:
                        raise ProviderError(ErrorKind.INVALID_RESPONSE, f"{e.error_count()} schema errors")
                except ProviderError as e:
                    llm_ms = (time.perf_counter() - t0) * 1000
                    last_kind = e.kind
                    attempts.append({"provider": provider.name, "attempt": attempt + 1,
                                     "error": e.kind.value, "ms": round(llm_ms, 1)})
                    METRICS.error(e.kind.value)
                    log_event("llm_error", request_id=request_id, provider=provider.name,
                              attempt=attempt + 1, error=e.kind.value)
                    if not e.kind.retryable:
                        break                              # e.g. bad API key: retrying won't help
                    if attempt < self.max_retries:
                        METRICS.incr("retries")
                        self._sleep(self.backoff_delay(attempt))
                    continue

                llm_ms = (time.perf_counter() - t0) * 1000
                cost = estimate_cost(resp.model, resp.input_tokens, resp.output_tokens)
                METRICS.record_latency("llm", llm_ms)
                METRICS.record_llm(resp.model, resp.input_tokens, resp.output_tokens, cost)
                attempts.append({"provider": provider.name, "attempt": attempt + 1, "error": None,
                                 "ms": round(llm_ms, 1)})
                if cache_key:
                    self.cache.set(cache_key, parsed.model_dump_json())
                total_ms = (time.perf_counter() - start) * 1000
                log_event("llm_success", request_id=request_id, provider=provider.name, model=resp.model,
                          input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                          cost_usd=cost, llm_ms=round(llm_ms, 1), attempts=len(attempts))
                return GatewayResult(output=parsed, request_id=request_id, model=resp.model,
                                     provider=provider.name, input_tokens=resp.input_tokens,
                                     output_tokens=resp.output_tokens, estimated_cost_usd=cost,
                                     latency_ms=round(total_ms, 1), fallback_used=p_index > 0,
                                     attempts=attempts)

        log_event("llm_all_failed", request_id=request_id, last_error=last_kind.value, attempts=len(attempts))
        raise GatewayError(last_kind, attempts)
