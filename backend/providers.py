"""LLM providers behind one common interface ("model abstraction", B1).

Every provider has the same method: generate(system, user, output_model, timeout_s) -> LLMResponse.
The gateway doesn't care whether it is talking to Claude, a mock, or a flaky simulator.
"""
import os
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Type

from pydantic import BaseModel

from backend import rule_extractor


class ErrorKind(str, Enum):
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"            # HTTP 429
    SERVER_ERROR = "SERVER_ERROR"        # HTTP 5xx
    CONNECTION = "CONNECTION"
    INVALID_RESPONSE = "INVALID_RESPONSE"  # bad JSON or fails the Pydantic schema
    AUTH = "AUTH"                        # bad / missing API key
    BAD_REQUEST = "BAD_REQUEST"          # our request was wrong (4xx)

    @property
    def retryable(self) -> bool:
        """Temporary problems are worth retrying; permanent ones are not."""
        return self in {ErrorKind.TIMEOUT, ErrorKind.RATE_LIMIT, ErrorKind.SERVER_ERROR,
                        ErrorKind.CONNECTION, ErrorKind.INVALID_RESPONSE}


class ProviderError(Exception):
    def __init__(self, kind: ErrorKind, message: str = ""):
        super().__init__(f"{kind.value}: {message}")
        self.kind = kind


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int


# Price per million tokens (input, output) in USD - from Anthropic's published pricing.
PRICING = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "mock-rule-based": (0.0, 0.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    inp, out = PRICING.get(model, (0.0, 0.0))
    return round(input_tokens / 1e6 * inp + output_tokens / 1e6 * out, 6)


class BaseProvider:
    name = "base"
    model = "base"

    def generate(self, system: str, user: str, output_model: Type[BaseModel],
                 timeout_s: float) -> LLMResponse:
        raise NotImplementedError


# ------------------------------------------------------------------ real provider

class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(self, model: Optional[str] = None, effort: str = "low"):
        import anthropic                      # imported here so tests don't need the SDK
        self._anthropic = anthropic
        self.model = model or os.getenv("LLM_MODEL", "claude-opus-5-5")
        self.effort = effort
        # max_retries=0: the GATEWAY owns retries/backoff so we can test and log them ourselves.
        # The key is read from the ANTHROPIC_API_KEY environment variable, never from code.
        self.client = anthropic.Anthropic(max_retries=0)

    def generate(self, system, user, output_model, timeout_s):
        a = self._anthropic
        try:
            resp = self.client.with_options(timeout=timeout_s).messages.parse(
                model=self.model,
                max_tokens=4000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=output_model,          # structured output: JSON matching the schema
                output_config={"effort": self.effort},
            )
        except a.APITimeoutError as e:
            raise ProviderError(ErrorKind.TIMEOUT, str(e))
        except a.RateLimitError as e:
            raise ProviderError(ErrorKind.RATE_LIMIT, str(e))
        except a.AuthenticationError as e:
            raise ProviderError(ErrorKind.AUTH, "authentication failed")   # never echo the key
        except a.APIStatusError as e:
            kind = ErrorKind.SERVER_ERROR if e.status_code >= 500 else ErrorKind.BAD_REQUEST
            raise ProviderError(kind, f"HTTP {e.status_code}")
        except a.APIConnectionError as e:
            raise ProviderError(ErrorKind.CONNECTION, str(e))
        except Exception as e:                                # SDK failed to parse/validate output
            raise ProviderError(ErrorKind.INVALID_RESPONSE, str(e)[:200])

        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            raise ProviderError(ErrorKind.INVALID_RESPONSE, f"stop_reason={resp.stop_reason}")
        return LLMResponse(text=resp.parsed_output.model_dump_json(), model=self.model,
                           provider=self.name, input_tokens=resp.usage.input_tokens,
                           output_tokens=resp.usage.output_tokens)


# ------------------------------------------------------------------ offline provider

class MockProvider(BaseProvider):
    """Deterministic, free, offline. Uses keyword rules instead of an LLM."""
    name = "mock"
    model = "mock-rule-based"

    def generate(self, system, user, output_model, timeout_s):
        text = rule_extractor.extract(user).model_dump_json()
        return LLMResponse(text=text, model=self.model, provider=self.name,
                           input_tokens=(len(system) + len(user)) // 4, output_tokens=len(text) // 4)


# ------------------------------------------------------------------ B3 failure simulator

class FlakyProvider(BaseProvider):
    """Wraps another provider and randomly injects failures (B3 reliability challenge)."""

    def __init__(self, inner: BaseProvider, p_timeout=0.15, p_429=0.15, p_500=0.1,
                 p_invalid_json=0.15, seed: Optional[int] = None, sleep_on_timeout: float = 0.0):
        self.inner = inner
        self.name = f"flaky({inner.name})"
        self.model = inner.model
        self.p = [(ErrorKind.TIMEOUT, p_timeout), (ErrorKind.RATE_LIMIT, p_429),
                  (ErrorKind.SERVER_ERROR, p_500), (ErrorKind.INVALID_RESPONSE, p_invalid_json)]
        self.rng = random.Random(seed)       # seed => reproducible tests
        self.sleep_on_timeout = sleep_on_timeout

    def generate(self, system, user, output_model, timeout_s):
        r, cumulative = self.rng.random(), 0.0
        for kind, prob in self.p:
            cumulative += prob
            if r < cumulative:
                if kind == ErrorKind.TIMEOUT:
                    time.sleep(self.sleep_on_timeout)
                    raise ProviderError(ErrorKind.TIMEOUT, "simulated timeout")
                if kind == ErrorKind.RATE_LIMIT:
                    raise ProviderError(ErrorKind.RATE_LIMIT, "simulated HTTP 429")
                if kind == ErrorKind.SERVER_ERROR:
                    raise ProviderError(ErrorKind.SERVER_ERROR, "simulated HTTP 500")
                # invalid JSON: return broken text and let the gateway's validator catch it
                good = self.inner.generate(system, user, output_model, timeout_s)
                return LLMResponse(text=good.text[: len(good.text) // 2], model=good.model,
                                   provider=self.name, input_tokens=good.input_tokens,
                                   output_tokens=good.output_tokens)
        return self.inner.generate(system, user, output_model, timeout_s)


class ScriptedProvider(BaseProvider):
    """Test helper: returns a fixed sequence of outcomes (errors or texts)."""
    name = "scripted"
    model = "mock-rule-based"

    def __init__(self, outcomes: list):
        self.outcomes = list(outcomes)
        self.calls = 0

    def generate(self, system, user, output_model, timeout_s):
        self.calls += 1
        item = self.outcomes.pop(0)
        if isinstance(item, ErrorKind):
            raise ProviderError(item, "scripted")
        return LLMResponse(text=item, model=self.model, provider=self.name, input_tokens=10, output_tokens=10)

