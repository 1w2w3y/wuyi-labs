"""OTLP/HTTP telemetry for a benchmark run. Standard library only.

Why hand-rolled rather than the OpenTelemetry SDK: the image otherwise needs
nothing from PyPI, and a load generator that cannot start because a telemetry
dependency failed to install is worse than one with no telemetry. OTLP/HTTP
accepts JSON as well as protobuf, and the JSON encoding is stable, so this is
a few hundred lines against a pip dependency tree. Everything here is
fail-soft: an export error is counted and logged once, never raised into the
run.

What it emits:

  traces   one span per LLM request, under a span per agent session, under one
           span for the run. The request span's `traceparent` goes out on the
           upstream call, so LiteLLM's own GenAI spans attach to the same
           trace and you get agent -> gateway -> model end to end.
  metrics  counters, gauges and histograms every `OTEL_FLUSH_SECONDS`, so a
           long run is watchable live rather than only at the end.
  logs     one record per finished session and one for the run summary.

Span and attribute names follow the GenAI semantic conventions where they
exist (`gen_ai.*`), so these spans sit alongside LiteLLM's in the same views.

One Azure Monitor quirk shapes the metric design: the metrics DCR in
observability/otelcol/config.yaml keeps only service.*, host.name and
cloud.region from the resource, and drops the rest. So every dimension that
matters -- run id, scenario, agent, model -- is set as a *data point*
attribute, not a resource attribute, or runs would merge into one series.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

SPAN_KIND_INTERNAL = 1
SPAN_KIND_CLIENT = 3
STATUS_OK = 1
STATUS_ERROR = 2
TEMPORALITY_CUMULATIVE = 2

SEVERITY_INFO = 9
SEVERITY_ERROR = 17

# Seconds. Wide, because an agent's prompt can be 200K tokens and its first
# token can take tens of seconds under load.
TTFT_BOUNDS = [0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120, 300]
DURATION_BOUNDS = [0.5, 1, 2, 5, 10, 20, 40, 80, 160, 320, 640]
PROMPT_TOKEN_BOUNDS = [1000, 4000, 8000, 16000, 32000, 64000, 128000, 200000, 400000]
TPS_BOUNDS = [1, 5, 10, 20, 30, 40, 60, 80, 120, 200]


def _now_ns():
    return time.time_ns()


def _attrs(mapping):
    """dict -> OTLP KeyValue list, skipping Nones."""
    out = []
    for key, value in (mapping or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            av = {"boolValue": value}
        elif isinstance(value, int):
            av = {"intValue": str(value)}
        elif isinstance(value, float):
            av = {"doubleValue": value}
        elif isinstance(value, (list, tuple)):
            av = {"arrayValue": {"values": [{"stringValue": str(v)} for v in value]}}
        else:
            av = {"stringValue": str(value)}
        out.append({"key": key, "value": av})
    return out


class Span:
    """A span that is recorded when it ends. Not thread-safe by itself."""

    __slots__ = ("telemetry", "trace_id", "span_id", "parent_id", "name",
                 "kind", "attributes", "start_ns", "links", "_ended")

    def __init__(self, telemetry, trace_id, span_id, parent_id, name, kind,
                 attributes, links=None):
        self.telemetry = telemetry
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_id = parent_id
        self.name = name
        self.kind = kind
        self.attributes = dict(attributes or {})
        self.links = list(links or [])
        self.start_ns = _now_ns()
        self._ended = False

    @property
    def traceparent(self):
        """W3C header so the next hop joins this trace. Sampled flag set."""
        return "00-{}-{}-01".format(self.trace_id, self.span_id)

    def set(self, **attributes):
        self.attributes.update(attributes)

    def end(self, error=None, **attributes):
        if self._ended:
            return
        self._ended = True
        self.attributes.update(attributes)
        if error:
            self.attributes.setdefault("error.type", str(error)[:200])
        record = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": self.kind,
            "startTimeUnixNano": str(self.start_ns),
            "endTimeUnixNano": str(_now_ns()),
            "attributes": _attrs(self.attributes),
            "status": {"code": STATUS_ERROR if error else STATUS_OK},
        }
        if self.links:
            record["links"] = [
                {"traceId": t, "spanId": sp} for t, sp in self.links if t and sp
            ]
        if self.parent_id:
            record["parentSpanId"] = self.parent_id
        self.telemetry._add_span(record)


class _Histogram:
    __slots__ = ("bounds", "buckets", "count", "total")

    def __init__(self, bounds):
        self.bounds = bounds
        self.buckets = [0] * (len(bounds) + 1)
        self.count = 0
        self.total = 0.0

    def observe(self, value):
        self.count += 1
        self.total += value
        for i, bound in enumerate(self.bounds):
            if value <= bound:
                self.buckets[i] += 1
                return
        self.buckets[-1] += 1


class Telemetry:
    """Buffers signals and ships them to an OTLP/HTTP endpoint.

    Disabled, and free, when no endpoint is configured: every method still
    works and returns a no-op span, so callers need no branches.
    """

    def __init__(self, endpoint="", headers=None, resource=None, dimensions=None,
                 flush_seconds=15.0, timeout=20.0, verbose=True, signal_urls=None,
                 auth=None):
        self.endpoint = (endpoint or "").rstrip("/")
        # Per-signal full URLs win over the base endpoint. Azure Monitor needs
        # this: its DCE puts traces and logs on the logs-ingest host and
        # metrics on a different metrics-ingest host, with a DCR id in each
        # path, so there is no common base to append /v1/traces to.
        self.signal_urls = dict(signal_urls or {})
        self.enabled = bool(self.endpoint or self.signal_urls)
        self.headers = dict(headers or {})
        self.resource = dict(resource or {})
        # Stamped on every metric data point; see the module docstring.
        self.dimensions = dict(dimensions or {})
        self.flush_seconds = flush_seconds
        self.timeout = timeout
        self.verbose = verbose
        self.auth = auth

        self._lock = threading.Lock()
        self._spans = []
        self._logs = []
        self._counters = {}
        self._gauges = {}
        self._histograms = {}
        self._start_ns = _now_ns()
        self._stop = threading.Event()
        self._thread = None
        self.exported = {"spans": 0, "metrics": 0, "logs": 0}
        self.failures = 0
        self._warned = False

    # -- lifecycle --------------------------------------------------------

    def start(self):
        if not self.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="agent-bench-otlp")
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(self.flush_seconds):
            self.flush()

    def shutdown(self):
        if not self.enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.timeout + 5)
        self.flush()

    # -- traces -----------------------------------------------------------

    def span(self, name, parent=None, kind=SPAN_KIND_INTERNAL, trace_id=None,
             new_trace=False, links=None, **attributes):
        """Start a span.

        `parent` is a Span or a (trace_id, span_id) pair. With `new_trace`,
        the span becomes the root of its own trace and merely *links* to the
        parent instead of nesting under it.

        That matters for a benchmark. Nesting everything under one run span
        would put every session and every request of a half-hour run in a
        single trace -- thousands of spans, which backends truncate, refuse,
        or render unusably. One trace per opencode session is the useful
        unit: it is what you open when a p95 TTFT looks wrong, and it is
        where the gateway's spans attach.
        """
        if not self.enabled:
            return _NullSpan()
        parent_trace, parent_span = None, None
        if isinstance(parent, Span):
            parent_trace, parent_span = parent.trace_id, parent.span_id
        elif isinstance(parent, tuple) and len(parent) == 2:
            parent_trace, parent_span = parent

        span_links = list(links or [])
        if new_trace:
            if parent_trace and parent_span:
                span_links.append((parent_trace, parent_span))
            parent_trace, parent_span = None, None

        tid = trace_id or parent_trace or secrets.token_hex(16)
        return Span(self, tid, secrets.token_hex(8), parent_span, name, kind,
                    attributes, span_links)

    def _add_span(self, record):
        with self._lock:
            self._spans.append(record)

    # -- metrics ----------------------------------------------------------

    def count(self, name, value=1, **attributes):
        if not self.enabled:
            return
        key = (name, _freeze(attributes))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def gauge(self, name, value, **attributes):
        if not self.enabled:
            return
        with self._lock:
            self._gauges[(name, _freeze(attributes))] = value

    def observe(self, name, value, bounds=None, **attributes):
        if not self.enabled or value is None:
            return
        key = (name, _freeze(attributes))
        with self._lock:
            hist = self._histograms.get(key)
            if hist is None:
                hist = self._histograms[key] = _Histogram(bounds or DURATION_BOUNDS)
            hist.observe(value)

    # -- logs -------------------------------------------------------------

    def log(self, body, severity=SEVERITY_INFO, span=None, **attributes):
        if not self.enabled:
            return
        record = {
            "timeUnixNano": str(_now_ns()),
            "severityNumber": severity,
            "severityText": "ERROR" if severity >= SEVERITY_ERROR else "INFO",
            "body": {"stringValue": str(body)},
            "attributes": _attrs({**self.dimensions, **attributes}),
        }
        if isinstance(span, Span):
            record["traceId"] = span.trace_id
            record["spanId"] = span.span_id
        with self._lock:
            self._logs.append(record)

    # -- export -----------------------------------------------------------

    def flush(self):
        if not self.enabled:
            return
        with self._lock:
            spans, self._spans = self._spans, []
            logs, self._logs = self._logs, []
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            hists = {k: (v.bounds, list(v.buckets), v.count, v.total)
                     for k, v in self._histograms.items()}

        if spans:
            self._post("traces", {"resourceSpans": [{
                "resource": {"attributes": _attrs(self.resource)},
                "scopeSpans": [{"scope": {"name": "agent-bench"}, "spans": spans}],
            }]}, "spans", len(spans))

        metrics = self._encode_metrics(counters, gauges, hists)
        if metrics:
            self._post("metrics", {"resourceMetrics": [{
                "resource": {"attributes": _attrs(self.resource)},
                "scopeMetrics": [{"scope": {"name": "agent-bench"}, "metrics": metrics}],
            }]}, "metrics", len(metrics))

        if logs:
            self._post("logs", {"resourceLogs": [{
                "resource": {"attributes": _attrs(self.resource)},
                "scopeLogs": [{"scope": {"name": "agent-bench"}, "logRecords": logs}],
            }]}, "logs", len(logs))

    def _encode_metrics(self, counters, gauges, hists):
        now = str(_now_ns())
        start = str(self._start_ns)
        out = []
        for (name, frozen), value in counters.items():
            out.append({"name": name, "sum": {
                "aggregationTemporality": TEMPORALITY_CUMULATIVE,
                "isMonotonic": True,
                "dataPoints": [{
                    "startTimeUnixNano": start, "timeUnixNano": now,
                    "asDouble": float(value),
                    "attributes": _attrs({**self.dimensions, **dict(frozen)}),
                }],
            }})
        for (name, frozen), value in gauges.items():
            out.append({"name": name, "gauge": {"dataPoints": [{
                "startTimeUnixNano": start, "timeUnixNano": now,
                "asDouble": float(value),
                "attributes": _attrs({**self.dimensions, **dict(frozen)}),
            }]}})
        for (name, frozen), (bounds, buckets, count, total) in hists.items():
            out.append({"name": name, "histogram": {
                "aggregationTemporality": TEMPORALITY_CUMULATIVE,
                "dataPoints": [{
                    "startTimeUnixNano": start, "timeUnixNano": now,
                    "count": str(count), "sum": total,
                    "bucketCounts": [str(b) for b in buckets],
                    "explicitBounds": [float(b) for b in bounds],
                    "attributes": _attrs({**self.dimensions, **dict(frozen)}),
                }],
            }})
        return out

    def _url_for(self, signal):
        override = self.signal_urls.get(signal)
        if override:
            return override
        if not self.endpoint:
            # Only some signals are configured; drop the rest rather than
            # posting to a relative path.
            return ""
        return "{}/v1/{}".format(self.endpoint, signal)

    def _post(self, signal, payload, kind, n):
        url = self._url_for(signal)
        if not url:
            return
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        headers.update(self.headers)
        if self.auth is not None and "Authorization" not in headers:
            try:
                headers["Authorization"] = self.auth.header()
            except Exception as exc:
                self._warn("could not get an Azure token: {}: {}".format(
                    type(exc).__name__, exc))
                return
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
            self.exported[kind] += n
        except urllib.error.HTTPError as exc:
            self._warn("{} export to {} rejected: HTTP {} {}".format(
                kind, url, exc.code,
                exc.read()[:200].decode("utf-8", "replace")))
        except Exception as exc:
            self._warn("{} export to {} failed: {}: {}".format(
                kind, url, type(exc).__name__, exc))

    def _warn(self, message):
        self.failures += 1
        # Once, so a dead collector cannot drown the run's own output.
        if self.verbose and not self._warned:
            self._warned = True
            print("telemetry: {} (further telemetry errors are counted, not "
                  "printed)".format(message), file=sys.stderr, flush=True)


class ManagedIdentityAuth:
    """Bearer tokens from Azure IMDS, for posting straight to Azure Monitor.

    The flash-next stack's collector authenticates to its data collection
    endpoint with the VM's user-assigned identity (Monitoring Metrics
    Publisher on the DCR). When agent-bench posts to that DCE itself rather
    than through the collector, it needs the same token. Only reachable from
    an Azure VM; anywhere else, pass a token in OTEL_EXPORTER_OTLP_HEADERS.
    """

    IMDS = ("http://169.254.169.254/metadata/identity/oauth2/token"
            "?api-version=2018-02-01&resource=https%3A%2F%2Fmonitor.azure.com%2F")

    def __init__(self, client_id=""):
        self.client_id = client_id
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    def header(self):
        with self._lock:
            # Refresh a couple of minutes early; tokens last an hour.
            if self._token and time.time() < self._expires - 120:
                return self._token
            url = self.IMDS
            if self.client_id:
                url += "&client_id=" + self.client_id
            req = urllib.request.Request(url, headers={"Metadata": "true"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            self._token = "Bearer " + data["access_token"]
            self._expires = time.time() + float(data.get("expires_in") or 3600)
            return self._token


class _NullSpan:
    """Stand-in when telemetry is off, so callers need no branches."""
    traceparent = ""
    trace_id = ""
    span_id = ""

    def set(self, **kwargs):
        pass

    def end(self, error=None, **kwargs):
        pass


def _freeze(mapping):
    return tuple(sorted((k, v) for k, v in (mapping or {}).items() if v is not None))


# -- session context ----------------------------------------------------
#
# The proxy sees requests but does not know which batch an agent is on, and
# the agent knows the batch but does not see requests. This registry joins
# them: the agent publishes its current session span, the proxy parents each
# request span onto it.

_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()


def set_session(agent, span):
    with _SESSIONS_LOCK:
        if span is None:
            _SESSIONS.pop(agent, None)
        else:
            _SESSIONS[agent] = (span.trace_id, span.span_id)


def get_session(agent):
    with _SESSIONS_LOCK:
        return _SESSIONS.get(agent)


# -- construction from the environment ----------------------------------


def from_env(run_id, scenario, model, extra_dimensions=None, verbose=True):
    """Build a Telemetry from OTEL_* environment variables.

    OTEL_EXPORTER_OTLP_ENDPOINT   base URL; /v1/traces etc. are appended
    OTEL_EXPORTER_OTLP_HEADERS    "k=v,k2=v2" (bearer tokens go here)
    OTEL_SERVICE_NAME             default "agent-bench"
    OTEL_RESOURCE_ATTRIBUTES      "k=v,k2=v2" merged into the resource
    OTEL_FLUSH_SECONDS            export interval, default 15
    AZMON_APPLICATION_ID          App Insights application id, when posting
                                  somewhere that does not add it for you
    """
    endpoint = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
                or os.environ.get("OTLP_ENDPOINT") or "").strip()
    signal_urls = {
        signal: (os.environ.get("OTEL_EXPORTER_OTLP_{}_ENDPOINT".format(signal.upper()))
                 # The flash-next stack already names these in its .env.
                 or os.environ.get("AZMON_{}_ENDPOINT".format(signal.upper()))
                 or "").strip()
        for signal in ("traces", "metrics", "logs")
    }
    signal_urls = {k: v for k, v in signal_urls.items() if v}
    if not endpoint and not signal_urls:
        return Telemetry()

    # A full signal URL is a common mistake; accept it and strip the suffix.
    for suffix in ("/v1/traces", "/v1/metrics", "/v1/logs"):
        if endpoint.rstrip("/").endswith(suffix):
            endpoint = endpoint.rstrip("/")[: -len(suffix)]
            break

    resource = {
        "service.name": os.environ.get("OTEL_SERVICE_NAME") or "agent-bench",
        "service.namespace": "llm-self-hosting",
        "service.version": os.environ.get("OPENCODE_VERSION"),
        "host.name": socket.gethostname(),
        "deployment.environment.name":
            os.environ.get("DEPLOYMENT_ENVIRONMENT") or "agent-bench",
    }
    if os.environ.get("AZMON_APPLICATION_ID"):
        resource["microsoft.applicationId"] = os.environ["AZMON_APPLICATION_ID"]
    resource.update(_parse_kv(os.environ.get("OTEL_RESOURCE_ATTRIBUTES")))

    dimensions = {
        "run_id": run_id,
        "scenario": scenario,
        # The same label the rest of the stack splits every signal by, so
        # agent-bench series line up with vLLM's and the gateway's.
        "upstream_model": os.environ.get("LLM_MODEL") or model,
    }
    dimensions.update(extra_dimensions or {})

    try:
        flush = float(os.environ.get("OTEL_FLUSH_SECONDS") or 15)
    except ValueError:
        flush = 15.0

    auth = None
    client_id = (os.environ.get("TELEMETRY_UAMI_CLIENT_ID")
                 or os.environ.get("AZURE_CLIENT_ID") or "")
    wants_azure = any("ingest.monitor.azure.com" in u for u in signal_urls.values())
    if wants_azure and "Authorization" not in _parse_kv(
            os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")):
        auth = ManagedIdentityAuth(client_id)

    return Telemetry(
        endpoint=endpoint,
        signal_urls=signal_urls,
        auth=auth,
        headers=_parse_kv(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")),
        resource=resource,
        dimensions=dimensions,
        flush_seconds=flush,
        verbose=verbose,
    )


def _parse_kv(raw):
    out = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        out[key.strip()] = value.strip()
    return out
