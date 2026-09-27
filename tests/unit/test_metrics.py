import pytest
from prometheus_client import CollectorRegistry

from gmail_automator.metrics import Metrics


def test_every_metric_family_is_registered_on_its_own_registry() -> None:
    metrics = Metrics()
    assert isinstance(metrics.registry, CollectorRegistry)
    # prometheus_client strips the `_total` suffix from a Counter's *family* name; the sample
    # keeps it. Both spellings appear below, which is the Prometheus convention.
    assert {family.name for family in metrics.registry.collect()} == {
        "gmail_automator_sends",
        "gmail_automator_send_failures",
        "gmail_automator_send_duration_seconds",
        "gmail_automator_queue_depth",
        "gmail_automator_quota_remaining",
        "gmail_automator_tokens_refreshed",
        "gmail_automator_worker_iterations",
        "gmail_automator_http_requests",
    }
    rendered = metrics.render()
    for sample in (
        "gmail_automator_sends_total",
        "gmail_automator_send_failures_total",
        "gmail_automator_tokens_refreshed_total",
        "gmail_automator_worker_iterations_total",
        "gmail_automator_http_requests_total",
    ):
        assert f"# TYPE {sample} counter" in rendered, sample


def test_two_metrics_instances_do_not_collide() -> None:
    """R11: the default registry raises on duplicate registration, and tests build many apps."""
    first, second = Metrics(), Metrics()
    assert first.registry is not second.registry
    first.record_send("me@example.com", "sent", source="api")
    second.record_send("me@example.com", "sent", source="api")
    assert "gmail_automator_sends_total" in first.render()
    assert "gmail_automator_sends_total" in second.render()


def test_record_send_labels_status_and_outcome() -> None:
    metrics = Metrics()
    metrics.record_send("me@example.com", "sent", source="api")
    metrics.record_send("me@example.com", "failed", source="api")
    metrics.record_send("me@example.com", "sent", source="mcp")
    text = metrics.render()
    assert (
        'gmail_automator_sends_total{account="me@example.com",outcome="sent",source="api"} 1.0'
        in text
    )
    assert (
        'gmail_automator_sends_total{account="me@example.com",outcome="failed",source="api"} 1.0'
        in text
    )
    assert 'source="mcp"' in text


def test_record_send_failure_increments_the_failure_counter() -> None:
    metrics = Metrics()
    metrics.record_send("me@example.com", "failed", source="api", error_code="gmail_rate_limited")
    text = metrics.render()
    assert "gmail_automator_send_failures_total" in text
    assert 'error_code="gmail_rate_limited"' in text


def test_observe_send_duration_records_histogram_buckets() -> None:
    metrics = Metrics()
    metrics.observe_send_duration("me@example.com", 0.25)
    metrics.observe_send_duration("me@example.com", 1.5)
    text = metrics.render()
    assert "gmail_automator_send_duration_seconds_bucket" in text
    assert 'gmail_automator_send_duration_seconds_count{account="me@example.com"} 2.0' in text


def test_set_queue_depth_is_a_gauge() -> None:
    metrics = Metrics()
    metrics.set_queue_depth(3)
    metrics.set_queue_depth(3)
    assert "gmail_automator_queue_depth 3.0" in metrics.render()
    metrics.set_queue_depth(1)
    assert "gmail_automator_queue_depth 1.0" in metrics.render()


def test_set_quota_remaining() -> None:
    metrics = Metrics()
    metrics.set_quota_remaining("me@example.com", "messages", 400)
    metrics.set_quota_remaining("me@example.com", "recipients", 380)
    text = metrics.render()
    assert (
        'gmail_automator_quota_remaining{account="me@example.com",resource="messages"} 400.0'
        in text
    )
    assert 'resource="recipients"} 380.0' in text


def test_record_token_refresh_and_worker_iteration() -> None:
    metrics = Metrics()
    metrics.record_token_refresh("me@example.com", "ok")
    metrics.record_token_refresh("me@example.com", "error")
    metrics.record_worker_iteration("sent")
    text = metrics.render()
    assert (
        'gmail_automator_tokens_refreshed_total{account="me@example.com",result="ok"} 1.0' in text
    )
    assert 'result="error"} 1.0' in text
    assert 'gmail_automator_worker_iterations_total{action="sent"} 1.0' in text


def test_record_http_request() -> None:
    metrics = Metrics()
    metrics.record_http_request("POST", "/v1/send", 200)
    metrics.record_http_request("POST", "/v1/send", 429)
    text = metrics.render()
    assert (
        'gmail_automator_http_requests_total{method="POST",path="/v1/send",status="200"} 1.0'
        in text
    )
    assert 'status="429"} 1.0' in text


def test_render_produces_valid_exposition_format() -> None:
    metrics = Metrics()
    metrics.record_send("me@example.com", "sent", source="api")
    rendered = metrics.render()
    for line in rendered.splitlines():
        if line.startswith("#"):
            assert line.startswith("# HELP ") or line.startswith("# TYPE ")


def test_record_send_rejects_an_unknown_outcome() -> None:
    metrics = Metrics()
    with pytest.raises(ValueError, match="outcome"):
        metrics.record_send("me@example.com", "exploded", source="api")


def test_content_type_is_the_prometheus_text_format() -> None:
    assert Metrics().content_type.startswith("text/plain")


def test_collect_returns_metric_families() -> None:
    metrics = Metrics()
    metrics.record_send("me@example.com", "sent", source="api")
    families = {family.name: family for family in metrics.registry.collect()}
    sample = families["gmail_automator_sends"].samples[0]
    assert sample.value == 1.0
    assert sample.name == "gmail_automator_sends_total"


def test_high_cardinality_account_label_is_the_only_label_on_gauges() -> None:
    """Account is bounded by the number of connected accounts, not by traffic."""
    metrics = Metrics()
    metrics.set_queue_depth(1)
    queue_family = next(
        family
        for family in metrics.registry.collect()
        if family.name == "gmail_automator_queue_depth"
    )
    assert queue_family.samples[0].labels == {}
