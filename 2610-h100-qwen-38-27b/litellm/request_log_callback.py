"""Emit metadata-only, trace-correlated LiteLLM request telemetry.

Prompt and response content, headers, query strings, raw provider payloads,
LiteLLM kwargs, tool arguments, and exception messages are intentionally never
serialized. The callback retains only operational metadata needed for metrics,
traces, and request correlation.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from litellm.integrations.custom_logger import CustomLogger
from prometheus_client import Counter, Histogram


_TRACEPARENT_RE = re.compile(
    r"^[0-9a-fA-F]{2}-([0-9a-fA-F]{32})-([0-9a-fA-F]{16})-[0-9a-fA-F]{2}$"
)
_METRIC_LABEL_NAMES = (
    "model",
    "response_model",
    "upstream_model",
    "outcome",
    "http_status",
    "method",
    "path",
    "stream",
)
_REQUESTS = Counter(
    "litellm_e2e_requests_total",
    "Completed LiteLLM E2E requests.",
    _METRIC_LABEL_NAMES,
)
_DURATION = Histogram(
    "litellm_e2e_request_duration_seconds",
    "End-to-end LiteLLM request duration in seconds.",
    _METRIC_LABEL_NAMES,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
)
_TTFT = Histogram(
    "litellm_e2e_time_to_first_token_seconds",
    "Time to first token for streaming LiteLLM requests.",
    _METRIC_LABEL_NAMES,
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
_TOKEN_BUCKETS = (
    1,
    4,
    8,
    16,
    32,
    64,
    128,
    256,
    512,
    1024,
    4096,
    16384,
    65536,
    262144,
    524288,
)
_INPUT_TOKENS = Histogram(
    "litellm_e2e_input_tokens",
    "Input tokens per LiteLLM request.",
    _METRIC_LABEL_NAMES,
    buckets=_TOKEN_BUCKETS,
)
_OUTPUT_TOKENS = Histogram(
    "litellm_e2e_output_tokens",
    "Output tokens per LiteLLM request.",
    _METRIC_LABEL_NAMES,
    buckets=_TOKEN_BUCKETS,
)
_TOTAL_TOKENS = Histogram(
    "litellm_e2e_total_tokens",
    "Total tokens per LiteLLM request.",
    _METRIC_LABEL_NAMES,
    buckets=_TOKEN_BUCKETS,
)
_PAYLOAD_BYTES = Histogram(
    "litellm_e2e_payload_bytes",
    "Serialized metadata-only completion event size in bytes.",
    (*_METRIC_LABEL_NAMES, "payload"),
    buckets=(
        256,
        512,
        1024,
        2048,
        4096,
        8192,
        16384,
        32768,
        65536,
        131072,
        262144,
        524288,
        1048576,
    ),
)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _integer(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _seconds(value: Any) -> Optional[float]:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _trace_context(kwargs: Mapping[str, Any]) -> tuple[Optional[str], Optional[str]]:
    litellm_params = _field(kwargs, "litellm_params", {}) or {}
    metadata = _field(litellm_params, "metadata", {}) or {}
    parent_span = _field(metadata, "litellm_parent_otel_span")

    if parent_span is not None:
        try:
            context = parent_span.get_span_context()
            if getattr(context, "is_valid", False) and context.trace_id:
                return (
                    format(context.trace_id, "032x"),
                    format(context.span_id, "016x"),
                )
        except (AttributeError, TypeError, ValueError):
            pass

    proxy_request = _field(litellm_params, "proxy_server_request", {}) or {}
    headers = _field(proxy_request, "headers", {}) or {}
    traceparent = _field(headers, "traceparent")
    match = _TRACEPARENT_RE.fullmatch(str(traceparent or ""))
    if match:
        return match.group(1).lower(), match.group(2).lower()

    return None, None


def _call_id(kwargs: Mapping[str, Any]) -> Optional[str]:
    standard_log = _field(kwargs, "standard_logging_object")
    value = (
        _field(standard_log, "litellm_call_id")
        or _field(kwargs, "litellm_call_id")
        or _field(kwargs, "litellm_call_id_for_streaming")
    )
    return str(value) if value is not None else None


def _http_status(kwargs: Mapping[str, Any], success: bool) -> int:
    if success:
        return 200
    exception = _field(kwargs, "exception") or _field(kwargs, "original_exception")
    candidate = _field(exception, "status_code") or _field(exception, "code")
    status = _integer(candidate)
    return status if status is not None and 100 <= status <= 599 else 500


def build_request_log_event(
    kwargs: Mapping[str, Any],
    response_obj: Any,
    start_time: Any,
    end_time: Any,
    *,
    success: bool,
) -> dict[str, Any]:
    """Build the metadata-only JSON event written to stdout and Loki."""

    trace_id, span_id = _trace_context(kwargs)
    start_seconds = _seconds(start_time)
    end_seconds = _seconds(end_time)
    duration_ms = None
    if start_seconds is not None and end_seconds is not None:
        duration_ms = round(max(0.0, end_seconds - start_seconds) * 1000, 3)
    completion_start_seconds = _seconds(_field(kwargs, "completion_start_time"))
    time_to_first_token_ms = None
    if start_seconds is not None and completion_start_seconds is not None:
        time_to_first_token_ms = round(
            max(0.0, completion_start_seconds - start_seconds) * 1000, 3
        )

    litellm_params = _field(kwargs, "litellm_params", {}) or {}
    proxy_request = _field(litellm_params, "proxy_server_request", {}) or {}
    optional_params = _field(kwargs, "optional_params", {}) or {}
    usage = _field(response_obj, "usage", {}) or {}
    exception = _field(kwargs, "exception") or _field(kwargs, "original_exception")
    url = _field(proxy_request, "url")
    timestamp = (
        end_time.astimezone(timezone.utc)
        if isinstance(end_time, datetime)
        else datetime.now(timezone.utc)
    )

    return {
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "event": "litellm_request_complete",
        "trace_id": trace_id,
        "span_id": span_id,
        "call_id": _call_id(kwargs),
        "outcome": "success" if success else "failure",
        "http_status": _http_status(kwargs, success),
        "method": _field(proxy_request, "method", "POST"),
        "path": urlsplit(str(url)).path if url else None,
        "model": _field(kwargs, "model"),
        "response_model": _field(response_obj, "model"),
        "upstream_model": os.getenv("LITELLM_ACTIVE_MODEL") or "unknown",
        "stream": bool(_field(optional_params, "stream", False)),
        "duration_ms": duration_ms,
        "time_to_first_token_ms": time_to_first_token_ms,
        "input_tokens": _integer(_field(usage, "prompt_tokens")),
        "output_tokens": _integer(_field(usage, "completion_tokens")),
        "total_tokens": _integer(_field(usage, "total_tokens")),
        "exception_type": type(exception).__name__ if exception is not None else None,
    }


class MetadataOnlyRequestLogCallback(CustomLogger):
    def __init__(self) -> None:
        super().__init__(turn_off_message_logging=True)

    @staticmethod
    def _serialize(event: Mapping[str, Any]) -> str:
        return json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _enrich_proxy_span(
        kwargs: Mapping[str, Any], event: Mapping[str, Any]
    ) -> None:
        litellm_params = _field(kwargs, "litellm_params", {}) or {}
        metadata = _field(litellm_params, "metadata", {}) or {}
        proxy_span = _field(metadata, "litellm_parent_otel_span")
        if proxy_span is None or not hasattr(proxy_span, "set_attribute"):
            return
        if hasattr(proxy_span, "is_recording") and not proxy_span.is_recording():
            return

        attributes = {
            "litellm.e2e.call_id": event.get("call_id"),
            "litellm.e2e.outcome": event.get("outcome"),
            "litellm.e2e.http_status": event.get("http_status"),
            "litellm.e2e.request.method": event.get("method"),
            "litellm.e2e.request.path": event.get("path"),
            "litellm.e2e.upstream_model": event.get("upstream_model"),
            "litellm.e2e.stream": event.get("stream"),
            "litellm.e2e.duration_ms": event.get("duration_ms"),
            "litellm.e2e.time_to_first_token_ms": event.get(
                "time_to_first_token_ms"
            ),
            "litellm.e2e.input_tokens": event.get("input_tokens"),
            "litellm.e2e.output_tokens": event.get("output_tokens"),
            "litellm.e2e.total_tokens": event.get("total_tokens"),
            "litellm.e2e.exception_type": event.get("exception_type"),
        }
        safe_attributes = {
            key: value for key, value in attributes.items() if value is not None
        }
        for key, value in safe_attributes.items():
            proxy_span.set_attribute(key, value)
        if hasattr(proxy_span, "add_event"):
            proxy_span.add_event("litellm.e2e.request_complete", safe_attributes)

    @staticmethod
    def _metric_labels(event: Mapping[str, Any]) -> dict[str, str]:
        return {
            "model": str(event.get("model") or "unknown"),
            "response_model": str(event.get("response_model") or "unknown"),
            "upstream_model": str(event.get("upstream_model") or "unknown"),
            "outcome": str(event.get("outcome") or "unknown"),
            "http_status": str(event.get("http_status") or "unknown"),
            "method": str(event.get("method") or "unknown"),
            "path": str(event.get("path") or "unknown"),
            "stream": str(bool(event.get("stream"))).lower(),
        }

    @classmethod
    def _record_metrics(cls, event: Mapping[str, Any], payload: str) -> None:
        labels = cls._metric_labels(event)
        trace_id = event.get("trace_id")
        exemplar = {"trace_id": str(trace_id)} if trace_id else None

        _REQUESTS.labels(**labels).inc(exemplar=exemplar)
        duration_ms = event.get("duration_ms")
        if isinstance(duration_ms, (int, float)):
            _DURATION.labels(**labels).observe(duration_ms / 1000.0, exemplar=exemplar)
        ttft_ms = event.get("time_to_first_token_ms")
        if event.get("stream") and isinstance(ttft_ms, (int, float)):
            _TTFT.labels(**labels).observe(ttft_ms / 1000.0, exemplar=exemplar)

        for metric, field in (
            (_INPUT_TOKENS, "input_tokens"),
            (_OUTPUT_TOKENS, "output_tokens"),
            (_TOTAL_TOKENS, "total_tokens"),
        ):
            value = event.get(field)
            if isinstance(value, (int, float)):
                metric.labels(**labels).observe(value, exemplar=exemplar)

        _PAYLOAD_BYTES.labels(**labels, payload="log_event").observe(
            len(payload.encode("utf-8")), exemplar=exemplar
        )

    @classmethod
    def _emit(cls, kwargs: Mapping[str, Any], event: Mapping[str, Any]) -> None:
        payload = cls._serialize(event)
        try:
            cls._record_metrics(event, payload)
        except Exception as error:
            print(
                cls._serialize(
                    {
                        "event": "litellm_metrics_recording_error",
                        "trace_id": event.get("trace_id"),
                        "error_type": type(error).__name__,
                    }
                ),
                flush=True,
            )
        try:
            cls._enrich_proxy_span(kwargs, event)
        except Exception as error:
            print(
                cls._serialize(
                    {
                        "event": "litellm_trace_enrichment_error",
                        "trace_id": event.get("trace_id"),
                        "error_type": type(error).__name__,
                    }
                ),
                flush=True,
            )
        print(payload, flush=True)

    async def async_log_success_event(
        self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        event = build_request_log_event(
            kwargs, response_obj, start_time, end_time, success=True
        )
        self._emit(kwargs, event)

    async def async_log_failure_event(
        self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        event = build_request_log_event(
            kwargs, response_obj, start_time, end_time, success=False
        )
        self._emit(kwargs, event)


proxy_handler_instance = MetadataOnlyRequestLogCallback()
