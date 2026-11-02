"""B2 + B3: structured-output validation and reliability under provider failures."""
import pytest

from backend.cache import TTLCache
from backend.gateway import GatewayError, LLMGateway
from backend.providers import ErrorKind, FlakyProvider, MockProvider, ScriptedProvider
from backend.schemas import StructuredAnalysis

VALID = MockProvider().generate("", "25 sqm outdoor balcony, budget $1000", StructuredAnalysis, 1).text
NO_SLEEP = lambda s: None   # noqa: E731  (tests should not really wait)


def gw(*providers, **kw):
    return LLMGateway(list(providers), sleep=NO_SLEEP, **kw)


def call(g, msg="25 sqm outdoor balcony"):
    return g.generate_structured(system="s", user=msg, output_model=StructuredAnalysis)


def test_retries_after_429_then_succeeds():
    p = ScriptedProvider([ErrorKind.RATE_LIMIT, ErrorKind.RATE_LIMIT, VALID])
    r = call(gw(p))
    assert p.calls == 3 and r.output.intent == "recommendation_request"
    assert [a["error"] for a in r.attempts] == ["RATE_LIMIT", "RATE_LIMIT", None]


def test_timeout_and_500_are_retried():
    p = ScriptedProvider([ErrorKind.TIMEOUT, ErrorKind.SERVER_ERROR, VALID])
    assert call(gw(p)).output is not None


def test_invalid_json_is_never_returned_to_app():
    broken = VALID[: len(VALID) // 2]                    # truncated JSON
    p = ScriptedProvider([broken, VALID])
    r = call(gw(p))
    assert r.attempts[0]["error"] == "INVALID_RESPONSE"
    assert isinstance(r.output, StructuredAnalysis)


def test_schema_violation_is_rejected():
    bad = VALID.replace('"confidence":0.5', '"confidence":7')   # confidence must be 0..1
    p = ScriptedProvider([bad, bad, bad, bad])
    with pytest.raises(GatewayError) as e:
        call(gw(p))
    assert e.value.kind == ErrorKind.INVALID_RESPONSE


def test_auth_error_is_not_retried_but_falls_back():
    primary = ScriptedProvider([ErrorKind.AUTH])
    r = call(gw(primary, MockProvider()))
    assert primary.calls == 1                             # no pointless retries
    assert r.fallback_used and r.provider == "mock"


def test_falls_back_after_retries_exhausted():
    primary = ScriptedProvider([ErrorKind.SERVER_ERROR] * 4)
    r = call(gw(primary, MockProvider(), max_retries=3))
    assert primary.calls == 4 and r.fallback_used


def test_all_providers_fail_raises_classified_error():
    with pytest.raises(GatewayError) as e:
        call(gw(ScriptedProvider([ErrorKind.TIMEOUT] * 4), max_retries=3))
    assert e.value.kind == ErrorKind.TIMEOUT and len(e.value.attempts) == 4


def test_backoff_grows_exponentially():
    g = gw(MockProvider(), base_delay_s=0.5, max_delay_s=8)
    delays = [g.backoff_delay(i) for i in range(5)]
    assert 0.5 <= delays[0] < 0.6 and 1.0 <= delays[1] < 1.1 and 8.0 <= delays[4] <= 8.8


def test_flaky_provider_200_requests_never_leak_bad_output():
    """B3 simulation: ~55% of calls fail randomly; the gateway must always return
    either valid output or a classified error - never broken data."""
    flaky = FlakyProvider(MockProvider(), seed=42)
    g = gw(flaky, max_retries=3)
    ok = failed = 0
    for _ in range(200):
        try:
            r = call(g)
            assert isinstance(r.output, StructuredAnalysis)
            ok += 1
        except GatewayError:
            failed += 1
    assert ok + failed == 200 and ok > 180


def test_cache_hit_skips_provider_and_pii_not_cached():
    p = ScriptedProvider([VALID])
    g = gw(p, cache=TTLCache())
    call(g, "outdoor tiles please"); r2 = call(g, "Outdoor   tiles please")
    assert r2.cached and p.calls == 1                      # normalised text => same key
    p2 = ScriptedProvider([VALID, VALID])
    g2 = gw(p2, cache=TTLCache())
    call(g2, "email me at a@b.com"); call(g2, "email me at a@b.com")
    assert p2.calls == 2                                   # personal data is never cached
