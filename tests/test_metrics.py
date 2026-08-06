"""Tests for src/observability/metrics.py — exposition format + correlation IDs."""
from __future__ import annotations

import logging
import math
import re
import threading

import pytest
from starlette.requests import Request

from src.observability.metrics import (
    CONTENT_TYPE_LATEST,
    MAX_SERIES_PER_METRIC,
    OVERFLOW_LABEL,
    REQUEST_ID_HEADER,
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    RequestIdFilter,
    RequestIDMiddleware,
    _fmt_float,
    get_request_id,
    metrics_endpoint,
    new_request_id,
    normalize_request_id,
    record_cache_hit,
    record_cache_miss,
    record_circuit_breaker_trip,
    record_rate_limit_throttled,
    record_search,
    register_collector,
    render_metrics,
    request_id_scope,
    reset_metrics,
    set_browser_pool_stats,
    set_cache_stats,
    track_search,
    unregister_collector,
)

_LABEL_PAIR = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
_UNESCAPE = {"n": "\n", "r": "\r", "\\": "\\", '"': '"'}


@pytest.fixture(autouse=True)
def _clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: _UNESCAPE.get(m.group(1), m.group(1)), text)


def _parse(payload: str) -> tuple[dict[str, str], dict[str, str], list[tuple[str, dict[str, str], float]]]:
    """Minimal Prometheus text-format parser: (help, type, samples)."""
    helps: dict[str, str] = {}
    types: dict[str, str] = {}
    samples: list[tuple[str, dict[str, str], float]] = []
    for line in payload.split("\n")[:-1]:  # payload always ends with a newline
        assert line, "blank lines are not valid exposition output"
        if line.startswith("# HELP "):
            name, _, text = line[len("# HELP "):].partition(" ")
            helps[name] = text
            continue
        if line.startswith("# TYPE "):
            name, _, kind = line[len("# TYPE "):].partition(" ")
            types[name] = kind
            continue
        assert not line.startswith("#"), f"unexpected comment: {line}"
        head, _, raw_value = line.rpartition(" ")
        name, brace, rest = head.partition("{")
        labels: dict[str, str] = {}
        if brace:
            assert rest.endswith("}"), f"unterminated label set: {line}"
            labels = {k: _unescape(v) for k, v in _LABEL_PAIR.findall(rest[:-1])}
        samples.append((name, labels, float(raw_value)))
    return helps, types, samples


def _find(samples, name, **labels):
    return [(n, lb, v) for n, lb, v in samples if n == name and all(lb.get(k) == v2 for k, v2 in labels.items())]


def test_counter_increments_and_label_isolation():
    c = Counter("t_requests_total", "doc", ("engine",))
    c.inc(engine="google")
    c.inc(2.0, engine="google")
    c.inc(engine="bing")
    assert c.value(engine="google") == 3.0
    assert c.value(engine="bing") == 1.0
    assert c.value(engine="never_touched") == 0.0
    with pytest.raises(ValueError):
        c.inc(-1.0, engine="google")
    with pytest.raises(ValueError):
        c.inc(1.0, wrong_label="x")


def test_counter_sync_is_reset_aware():
    c = Counter("t_cache_hits_total", "doc", ("cache",))
    c.sync(10, cache="serp")
    assert c.value(cache="serp") == 10.0
    c.sync(14, cache="serp")  # source advanced by 4
    assert c.value(cache="serp") == 14.0
    c.sync(3, cache="serp")  # source restarted: add its new total, never rewind
    assert c.value(cache="serp") == 17.0


def test_gauge_set_inc_dec():
    g = Gauge("t_active_tabs", "doc")
    assert g.value() == 0.0
    g.set(7)
    g.inc(3)
    g.dec(4.5)
    assert g.value() == 5.5


def test_histogram_buckets_are_cumulative_with_sum_and_count():
    h = Histogram("t_latency_seconds", "doc", ("engine",), (0.1, 0.5, 1.0, 2.0))
    for value in (0.05, 0.4, 0.5, 1.5, 9.0):
        h.observe(value, engine="google")

    reg = MetricsRegistry()
    reg._get_or_create(h)
    _, types, samples = _parse(reg.render())
    assert types["t_latency_seconds"] == "histogram"

    buckets = [(lb["le"], v) for _, lb, v in _find(samples, "t_latency_seconds_bucket", engine="google")]
    assert [le for le, _ in buckets] == ["0.1", "0.5", "1.0", "2.0", "+Inf"]
    counts = [v for _, v in buckets]
    assert counts == [1.0, 3.0, 3.0, 4.0, 5.0]
    assert all(b >= a for a, b in zip(counts, counts[1:])), "buckets must be cumulative"

    (_, _, total) = _find(samples, "t_latency_seconds_sum", engine="google")[0]
    (_, _, count) = _find(samples, "t_latency_seconds_count", engine="google")[0]
    assert total == pytest.approx(0.05 + 0.4 + 0.5 + 1.5 + 9.0)
    assert count == 5.0
    assert counts[-1] == count, "+Inf bucket must equal _count"
    assert h.sample_count(engine="google") == 5
    assert h.sample_sum(engine="google") == pytest.approx(11.45)


def test_exposition_structure_and_trailing_newline():
    record_search("google", "success", 1.25)
    record_search("bing", "timeout", 25.0)
    payload = render_metrics()

    assert payload.endswith("\n")
    assert "\n\n" not in payload
    helps, types, samples = _parse(payload)

    assert types["wsm_search_requests_total"] == "counter"
    assert types["wsm_search_latency_seconds"] == "histogram"
    assert types["wsm_browser_pool_active_tabs"] == "gauge"
    assert helps["wsm_search_requests_total"]
    # exactly one HELP and one TYPE per family
    for line_prefix in ("# HELP wsm_search_requests_total ", "# TYPE wsm_search_requests_total "):
        assert payload.count(line_prefix) == 1

    assert _find(samples, "wsm_search_requests_total", engine="google", outcome="success")[0][2] == 1.0
    assert _find(samples, "wsm_search_requests_total", engine="bing", outcome="timeout")[0][2] == 1.0
    # the 25s observation lands in the last finite bucket and in +Inf
    inf_bucket = _find(samples, "wsm_search_latency_seconds_bucket", engine="bing", le="+Inf")[0][2]
    assert inf_bucket == 1.0


def test_label_and_help_escaping():
    c = Counter("t_escaped_total", "line one\\two\nline three", ("raw",))
    c.inc(raw='we"ird\\path\nnext')
    reg = MetricsRegistry()
    reg._get_or_create(c)
    payload = reg.render()

    assert "# HELP t_escaped_total line one\\\\two\\nline three\n" in payload
    assert 't_escaped_total{raw="we\\"ird\\\\path\\nnext"} 1.0\n' in payload
    _, _, samples = _parse(payload)
    assert samples[0][1]["raw"] == 'we"ird\\path\nnext'


def test_cardinality_guard_folds_into_overflow_series():
    c = Counter("t_bounded_total", "doc", ("kind",))
    for i in range(MAX_SERIES_PER_METRIC + 50):
        c.inc(kind=f"kind-{i}")
    reg = MetricsRegistry()
    reg._get_or_create(c)
    _, _, samples = _parse(reg.render())

    assert len(samples) == MAX_SERIES_PER_METRIC + 1  # capped + one overflow bucket
    assert _find(samples, "t_bounded_total", kind=OVERFLOW_LABEL)[0][2] == 50.0
    # long values are truncated so a stray query string cannot bloat the payload
    c2 = Counter("t_long_total", "doc", ("v",))
    c2.inc(v="x" * 500)
    assert len(next(iter(c2._values))[0]) == 48


def test_engine_and_outcome_labels_are_normalized():
    record_search("Google", "success")
    record_search("some-random-engine", "exploded")
    record_circuit_breaker_trip("yandex")
    _, _, samples = _parse(render_metrics())

    assert _find(samples, "wsm_search_requests_total", engine="google", outcome="success")[0][2] == 1.0
    assert _find(samples, "wsm_search_requests_total", engine="other", outcome="other")[0][2] == 1.0
    assert _find(samples, "wsm_engine_circuit_breaker_trips_total", engine="other")[0][2] == 1.0


def test_cache_pool_and_throttle_helpers():
    record_cache_hit("serp")
    record_cache_hit("serp")
    record_cache_miss("serp")
    set_cache_stats(hits=5, misses=2, cache="page")
    record_rate_limit_throttled("per_key", 3)
    set_browser_pool_stats(
        {"active_tabs": 4, "pool_size": 18, "max_pool_size": 36, "generation": 2, "started": True}
    )
    _, _, samples = _parse(render_metrics())

    assert _find(samples, "wsm_cache_hits_total", cache="serp")[0][2] == 2.0
    assert _find(samples, "wsm_cache_misses_total", cache="serp")[0][2] == 1.0
    assert _find(samples, "wsm_cache_hits_total", cache="page")[0][2] == 5.0
    assert _find(samples, "wsm_rate_limit_throttled_total", scope="per_key")[0][2] == 3.0
    assert _find(samples, "wsm_browser_pool_active_tabs")[0][2] == 4.0
    assert _find(samples, "wsm_browser_pool_size")[0][2] == 18.0
    assert _find(samples, "wsm_browser_pool_max_size")[0][2] == 36.0
    assert _find(samples, "wsm_browser_pool_generation")[0][2] == 2.0


def test_collector_runs_at_render_time():
    calls: list[int] = []

    def collect() -> None:
        calls.append(1)
        set_browser_pool_stats({"active_tabs": 9})

    register_collector(collect)
    try:
        _, _, samples = _parse(render_metrics())
    finally:
        unregister_collector(collect)
    assert calls == [1]
    assert _find(samples, "wsm_browser_pool_active_tabs")[0][2] == 9.0

    def boom() -> None:
        raise RuntimeError("collector down")

    register_collector(boom)
    try:
        assert render_metrics().endswith("\n")  # a broken collector never breaks the scrape
    finally:
        unregister_collector(boom)


def test_concurrent_increments_are_lossless():
    c = Counter("t_threaded_total", "doc", ("engine",))

    def worker() -> None:
        for _ in range(500):
            c.inc(engine="google")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.value(engine="google") == 4000.0


async def test_track_search_records_outcome_and_latency():
    async with track_search("duckduckgo") as tracker:
        tracker.outcome = "blocked"
    with pytest.raises(TimeoutError):
        async with track_search("google"):
            raise TimeoutError
    _, _, samples = _parse(render_metrics())

    assert _find(samples, "wsm_search_requests_total", engine="duckduckgo", outcome="blocked")[0][2] == 1.0
    assert _find(samples, "wsm_search_requests_total", engine="google", outcome="timeout")[0][2] == 1.0
    assert _find(samples, "wsm_search_latency_seconds_count", engine="google")[0][2] == 1.0


def test_counter_inc_rejects_non_finite_and_series_stays_intact():
    c = Counter("t_nonfinite_total", "doc", ("k",))
    c.inc(2.0, k="a")
    c.inc(math.nan, k="a")
    c.inc(math.inf, k="a")
    assert c.value(k="a") == 2.0
    with pytest.raises(ValueError):  # -Inf is negative first and foremost
        c.inc(-math.inf, k="a")
    assert c.value(k="a") == 2.0
    reg = MetricsRegistry()
    reg._get_or_create(c)
    assert 't_nonfinite_total{k="a"} 2.0\n' in reg.render()


def test_counter_sync_rejects_non_finite_and_keeps_baseline():
    c = Counter("t_sync_nonfinite_total", "doc", ("cache",))
    c.sync(10, cache="serp")
    c.sync(math.nan, cache="serp")
    c.sync(math.inf, cache="serp")
    c.sync(-math.inf, cache="serp")
    assert c.value(cache="serp") == 10.0
    c.sync(14, cache="serp")  # delta still computed from the last good total
    assert c.value(cache="serp") == 14.0


def test_gauge_rejects_non_finite():
    g = Gauge("t_gauge_nonfinite", "doc")
    g.set(5.0)
    g.set(math.nan)
    g.set(math.inf)
    g.set(-math.inf)
    g.inc(math.nan)
    g.dec(math.inf)
    assert g.value() == 5.0


def test_non_finite_drop_warns_once_per_family(caplog):
    c = Counter("t_warn_once_total", "doc")
    with caplog.at_level(logging.WARNING, logger="src.observability.metrics"):
        c.inc(math.nan)
        c.inc(math.inf)
        c.inc(math.nan)
    assert len([r for r in caplog.records if "t_warn_once_total" in r.getMessage()]) == 1
    assert c.value() == 0.0


def test_histogram_still_drops_nan_but_observes_inf():
    h = Histogram("t_nan_hist_seconds", "doc", buckets=(1.0,))
    h.observe(0.5)
    h.observe(math.nan)
    assert h.sample_count() == 1
    assert h.sample_sum() == 0.5
    h.observe(math.inf)  # unchanged semantics: +Inf lands in the +Inf bucket
    assert h.sample_count() == 2


def test_fmt_float_canonical_go_exponents():
    # the reproduced defect: >= 1e10 rendered as 1e+010 instead of 1e+10
    assert _fmt_float(1e10) == "1e+10"
    assert _fmt_float(1.5e10) == "1.5e+10"
    assert _fmt_float(1e12) == "1e+12"
    assert _fmt_float(1e15) == "1e+15"
    # single-digit exponents keep Go's two-digit zero padding
    assert _fmt_float(1e6) == "1e+06"
    assert _fmt_float(1e7) == "1e+07"
    assert _fmt_float(12345678.9) == "1.23456789e+07"
    # Python repr is already exponent-form at 1e16 and passes through untouched
    assert _fmt_float(1e16) == "1e+16"
    # special values per the exposition spec
    assert _fmt_float(math.inf) == "+Inf"
    assert _fmt_float(-math.inf) == "-Inf"
    assert _fmt_float(math.nan) == "NaN"
    # small values keep their existing plain rendering
    assert _fmt_float(0.25) == "0.25"
    assert _fmt_float(1.0) == "1.0"


def test_large_counter_value_renders_canonically():
    c = Counter("t_huge_total", "doc")
    c.inc(1e10)
    reg = MetricsRegistry()
    reg._get_or_create(c)
    payload = reg.render()
    assert "t_huge_total 1e+10\n" in payload
    assert _parse(payload)[2][0][2] == 1e10


def test_carriage_return_in_label_value_is_escaped():
    c = Counter("t_cr_total", "doc", ("raw",))
    c.inc(raw="line1\rline2")
    reg = MetricsRegistry()
    reg._get_or_create(c)
    payload = reg.render()
    assert "\r" not in payload, "raw CR must never reach the exposition output"
    assert 't_cr_total{raw="line1\\rline2"} 1.0\n' in payload
    _, _, samples = _parse(payload)
    assert samples[0][1]["raw"] == "line1\rline2"


def test_histogram_reregistration_with_different_buckets_raises():
    reg = MetricsRegistry()
    h1 = reg.histogram("t_rereg_seconds", "doc", ("engine",), (0.1, 0.5, 1.0))
    assert reg.histogram("t_rereg_seconds", "doc", ("engine",), (0.1, 0.5, 1.0)) is h1
    # the +Inf tail is implicit — spelling it out is the same identity
    assert reg.histogram("t_rereg_seconds", "doc", ("engine",), (0.1, 0.5, 1.0, math.inf)) is h1
    with pytest.raises(ValueError, match="different buckets"):
        reg.histogram("t_rereg_seconds", "doc", ("engine",), (0.25, 2.0))
    # pre-existing shape conflicts still raise
    with pytest.raises(ValueError, match="different shape"):
        reg.counter("t_rereg_seconds", "doc", ("engine",))
    with pytest.raises(ValueError, match="different shape"):
        reg.histogram("t_rereg_seconds", "doc", ("other",), (0.1, 0.5, 1.0))


def test_render_stays_valid_exposition_after_non_finite_attempts():
    set_browser_pool_stats({"active_tabs": 4})
    record_search("google", "success", 0.7)
    record_search("google", "success", float("nan"))  # latency dropped, count kept
    set_cache_stats(hits=3, misses=1, cache="page")
    set_cache_stats(hits=math.inf, misses=math.nan, cache="page")  # both dropped
    set_browser_pool_stats({"active_tabs": math.nan})

    payload = render_metrics()
    assert payload.endswith("\n")
    helps, types, samples = _parse(payload)

    for family in types:
        assert family in helps
        assert payload.count(f"# HELP {family} ") == 1
        assert payload.count(f"# TYPE {family} ") == 1
    for name, labels, value in samples:
        assert math.isfinite(value), f"{name}{labels} rendered non-finite"

    assert _find(samples, "wsm_search_requests_total", engine="google", outcome="success")[0][2] == 2.0
    buckets = [v for _, _, v in _find(samples, "wsm_search_latency_seconds_bucket", engine="google")]
    assert buckets == sorted(buckets), "buckets must stay cumulative and monotonic"
    count = _find(samples, "wsm_search_latency_seconds_count", engine="google")[0][2]
    total = _find(samples, "wsm_search_latency_seconds_sum", engine="google")[0][2]
    assert buckets[-1] == count == 1.0
    assert total == pytest.approx(0.7)
    assert _find(samples, "wsm_cache_hits_total", cache="page")[0][2] == 3.0
    assert _find(samples, "wsm_cache_misses_total", cache="page")[0][2] == 1.0
    assert _find(samples, "wsm_browser_pool_active_tabs")[0][2] == 4.0


def test_request_id_generation_normalization_and_scope():
    assert get_request_id() == ""
    generated = new_request_id()
    assert len(generated) == 12 and generated.isalnum()
    assert new_request_id() != generated

    assert normalize_request_id("abc-123_XY:Z.1") == "abc-123_XY:Z.1"
    assert normalize_request_id(None) != ""
    assert normalize_request_id("bad value\r\nInjected: 1") not in ("bad value\r\nInjected: 1", "")
    assert len(normalize_request_id("x" * 65)) == 12  # too long -> fresh id

    with request_id_scope("inbound-1") as rid:
        assert rid == "inbound-1"
        assert get_request_id() == "inbound-1"
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "inbound-1"
    assert get_request_id() == ""

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
    RequestIdFilter().filter(record)
    assert record.request_id == "-"


async def test_request_id_middleware_propagates_header():
    seen: list[str] = []
    sent: list[dict] = []

    async def app(scope, receive, send):
        seen.append(get_request_id())
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        sent.append(message)

    async def receive():  # pragma: no cover - never awaited by this app
        return {"type": "http.request", "body": b""}

    middleware = RequestIDMiddleware(app)
    scope = {"type": "http", "method": "GET", "path": "/mcp", "headers": [(b"x-request-id", b"trace-42")]}
    await middleware(scope, receive, send)

    assert seen == ["trace-42"]
    headers = dict(sent[0]["headers"])
    assert headers[REQUEST_ID_HEADER.lower().encode()] == b"trace-42"
    assert get_request_id() == "", "the scope must be reset after the response"

    # no inbound header -> a generated id is still echoed back
    sent.clear()
    seen.clear()
    await middleware({"type": "http", "method": "GET", "path": "/mcp", "headers": []}, receive, send)
    assert len(seen[0]) == 12
    assert dict(sent[0]["headers"])[REQUEST_ID_HEADER.lower().encode()].decode() == seen[0]


async def test_metrics_endpoint_content_type_and_body():
    record_search("bing", "success", 0.3)
    request = Request({"type": "http", "method": "GET", "path": "/metrics", "headers": []})
    response = await metrics_endpoint(request)

    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    body = response.body.decode()
    assert body.startswith("# HELP wsm_search_requests_total ")
    assert 'wsm_search_requests_total{engine="bing",outcome="success"} 1.0\n' in body
    assert math.isclose(_parse(body)[2][0][2], 1.0)
