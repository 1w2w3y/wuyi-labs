"""Metering reverse proxy between the agents and the LLM gateway.

Every agent talks to its own loopback port here, and the proxy forwards to the
real gateway while recording what crossed the wire: prompt and completion
tokens, time to first token, inter-token latency, tool calls and the files the
agent read. One JSON line per request.

Metering here rather than inside the agent is deliberate. opencode's
`--format json` emitted nothing on runs that otherwise worked, and its event
shape is not a documented interface, so anything built on it breaks on
upgrade. The wire format is OpenAI's and stable, the timings are the ones the
agent actually saw, and the same harness meters any agent CLI that speaks
OpenAI.

The proxy adds `stream_options.include_usage` to streaming requests so the
gateway returns a final usage chunk with exact token counts. Agents ignore
that chunk: it carries no choices.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import telemetry as telemetry_mod

CHAT_PATHS = ("/v1/chat/completions", "/chat/completions")

_inflight = 0
_inflight_lock = threading.Lock()


def inflight():
    """Requests currently open upstream, across every agent."""
    with _inflight_lock:
        return _inflight


def _inflight_add(delta):
    global _inflight
    with _inflight_lock:
        _inflight += delta
READ_TOOLS = ("read", "Read", "view", "cat")

# opencode's read tool wraps a file in these tags; a refusal or an error comes
# back as plain text in the same place. Matching the success shape rather than
# the error strings means a format change in a new opencode shows up as files
# unread, which is loud, and not as a review that never happened.
READ_RESULT = re.compile(r"\s*<path>(.+?)</path>\s*<type>file</type>")


class Recorder:
    """Append-only JSONL sink shared by every proxy thread.

    Also keeps, per agent, the set of paths whose reads returned the file. The
    orchestrator reads this back after each session to tell "reviewed" from
    "declared done without looking", and to re-drive what was skipped.
    """

    def __init__(self, path):
        self._fh = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()
        self._reads = {}
        self.count = 0

    def write(self, row):
        line = json.dumps(row, separators=(",", ":"))
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            self.count += 1

    def confirm_reads(self, agent, paths):
        """Note reads that returned a file. Returns the ones not seen before."""
        with self._lock:
            known = self._reads.setdefault(agent or "", set())
            new = [p for p in dict.fromkeys(paths) if p not in known]
            known.update(new)
            return new

    def reads_for(self, agent):
        with self._lock:
            return set(self._reads.get(agent, ()))

    def close(self):
        with self._lock:
            self._fh.close()


def _collapse_tool_deltas(acc):
    """Collapse streamed tool_call deltas into [{name, arguments}]."""
    return [{"name": acc[i][0], "arguments": acc[i][1]} for i in sorted(acc)]


def confirmed_reads(messages):
    """File paths that read calls actually returned, from a request's history.

    A tool call only says what the agent asked for. Its result comes back in
    the next request as a `tool` message, the first point on the wire where a
    read can be told from a denied or failed one. The path is the one the
    result names, resolved by the agent.
    """
    paths = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "tool" and isinstance(content, str):
            match = READ_RESULT.match(content)
            if match:
                paths.append(match.group(1))
    return paths


def read_paths(tool_calls):
    """File paths the agent asked to read, whether or not it got them."""
    paths = []
    for call in tool_calls:
        if call.get("name") not in READ_TOOLS:
            continue
        try:
            args = json.loads(call.get("arguments") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(args, dict):
            continue
        path = args.get("filePath") or args.get("file_path") or args.get("path")
        if path:
            paths.append(path)
    return paths


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Bound per server by serve():
    upstream = ""
    api_key = ""
    recorder = None
    agent = ""
    telemetry = None

    def log_message(self, fmt, *args):
        pass  # quiet: the runner owns stdout

    def do_GET(self):
        self._forward(b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._forward(self.rfile.read(length) if length else b"")

    # -- plumbing ---------------------------------------------------------

    def _upstream_request(self, body, span=None):
        url = self.upstream.rstrip("/") + self.path
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        # LiteLLM extracts an inbound traceparent and parents its own GenAI
        # spans onto it, so the gateway's and the model's work land in this
        # request's trace instead of a separate root.
        if span is not None and getattr(span, "traceparent", ""):
            headers["traceparent"] = span.traceparent
        return urllib.request.Request(
            url, data=body or None, headers=headers, method=self.command
        )

    def _forward(self, body):
        is_chat = any(self.path.endswith(p) for p in CHAT_PATHS)
        payload = None
        if is_chat and body:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None

        streaming = bool(payload and payload.get("stream"))
        if streaming:
            opts = dict(payload.get("stream_options") or {})
            opts["include_usage"] = True
            payload["stream_options"] = opts
            body = json.dumps(payload).encode()

        model = (payload or {}).get("model")
        row = {
            "agent": self.agent,
            "t_start": time.time(),
            "path": self.path,
            "model": model,
            "stream": streaming,
            "n_messages": len(((payload or {}).get("messages")) or []),
            "n_tools_offered": len(((payload or {}).get("tools")) or []),
        }
        # Noted on arrival rather than with the row, which is written when this
        # request ends: that can be after the session that needs it has ended.
        if self.recorder is not None:
            row["read_ok_paths"] = self.recorder.confirm_reads(
                self.agent, confirmed_reads((payload or {}).get("messages") or []))
        started = time.monotonic()

        span = None
        if is_chat and self.telemetry is not None:
            span = self.telemetry.span(
                "chat {}".format(model or "unknown"),
                parent=telemetry_mod.get_session(self.agent),
                kind=telemetry_mod.SPAN_KIND_CLIENT,
                **{
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": model,
                    "gen_ai.request.max_tokens": (payload or {}).get("max_tokens"),
                    "gen_ai.request.temperature": (payload or {}).get("temperature"),
                    "agent_bench.agent": self.agent,
                    "agent_bench.messages": row["n_messages"],
                    "agent_bench.tools_offered": row["n_tools_offered"],
                    "server.address": self.upstream,
                }
            )
            row["trace_id"] = span.trace_id
            row["span_id"] = span.span_id

        _inflight_add(1)
        try:
            resp = urllib.request.urlopen(
                self._upstream_request(body, span), timeout=1800)
        except urllib.error.HTTPError as exc:
            _inflight_add(-1)
            detail = exc.read()
            row.update(
                status=exc.code,
                error=detail[:500].decode("utf-8", "replace"),
                duration_s=round(time.monotonic() - started, 3),
            )
            self._record(row)
            self._finish_span(span, row, error="HTTP {}".format(exc.code))
            self._send_raw(exc.code, detail,
                           exc.headers.get("Content-Type", "application/json"))
            return
        except Exception as exc:  # reset, DNS, timeout
            _inflight_add(-1)
            row.update(
                status=0,
                error="{}: {}".format(type(exc).__name__, exc),
                duration_s=round(time.monotonic() - started, 3),
            )
            self._record(row)
            self._finish_span(span, row, error=type(exc).__name__)
            self._send_raw(502, json.dumps({"error": str(exc)}).encode(),
                           "application/json")
            return

        try:
            if streaming:
                self._pump_sse(resp, row, started, span)
            else:
                self._pump_whole(resp, row, started, span)
        finally:
            _inflight_add(-1)

    def _finish_span(self, span, row, error=None):
        """End the request span and feed the run's metrics from the same row."""
        tel = self.telemetry
        if span is None or tel is None:
            return
        span.end(
            error=error,
            **{
                "gen_ai.response.model": row.get("model"),
                "gen_ai.usage.input_tokens": row.get("prompt_tokens"),
                "gen_ai.usage.output_tokens": row.get("completion_tokens"),
                "gen_ai.response.finish_reasons": [row["finish_reason"]]
                    if row.get("finish_reason") else None,
                "agent_bench.cached_prompt_tokens": row.get("cached_prompt_tokens"),
                "agent_bench.reasoning_tokens": row.get("reasoning_tokens"),
                "agent_bench.ttft_seconds": row.get("ttft_s"),
                "agent_bench.tool_calls": row.get("tool_calls") or None,
                "agent_bench.files_read": len(row.get("read_ok_paths") or []) or None,
                "http.response.status_code": row.get("status"),
            }
        )

        agent = self.agent
        tel.count("agent_bench.requests", 1, agent=agent,
                  status=str(row.get("status")),
                  outcome="error" if error else "ok")
        if error:
            return
        for kind, key in (("input", "prompt_tokens"),
                          ("output", "completion_tokens"),
                          ("cached", "cached_prompt_tokens"),
                          ("reasoning", "reasoning_tokens")):
            value = row.get(key)
            if value:
                tel.count("agent_bench.tokens", value, agent=agent, kind=kind)
        for name in row.get("tool_calls") or []:
            tel.count("agent_bench.tool_calls", 1, agent=agent, tool=str(name))
        tel.observe("agent_bench.ttft", row.get("ttft_s"),
                    bounds=telemetry_mod.TTFT_BOUNDS, agent=agent)
        tel.observe("agent_bench.request.duration", row.get("duration_s"),
                    bounds=telemetry_mod.DURATION_BOUNDS, agent=agent)
        tel.observe("agent_bench.prompt.tokens", row.get("prompt_tokens"),
                    bounds=telemetry_mod.PROMPT_TOKEN_BOUNDS, agent=agent)
        completion, duration, ttft = (row.get("completion_tokens"),
                                      row.get("duration_s"), row.get("ttft_s"))
        if completion and duration:
            decode = duration - (ttft or 0)
            if decode > 0.05:
                tel.observe("agent_bench.per_stream_tps", completion / decode,
                            bounds=telemetry_mod.TPS_BOUNDS, agent=agent)

    def _record(self, row):
        if self.recorder is not None:
            self.recorder.write(row)

    def _send_raw(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _pump_whole(self, resp, row, started, span=None):
        body = resp.read()
        row["status"] = resp.status
        row["duration_s"] = round(time.monotonic() - started, 3)
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            self._record_usage(row, data.get("usage") or {})
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            calls = [
                {
                    "name": (t.get("function") or {}).get("name"),
                    "arguments": (t.get("function") or {}).get("arguments"),
                }
                for t in (message.get("tool_calls") or [])
            ]
            row["tool_calls"] = [c["name"] for c in calls]
            row["read_paths"] = read_paths(calls)
            row["finish_reason"] = choice.get("finish_reason")
            row["content_chars"] = len(message.get("content") or "")
        self._record(row)
        self._finish_span(span, row)
        self._send_raw(resp.status, body,
                       resp.headers.get("Content-Type", "application/json"))

    def _pump_sse(self, resp, row, started, span=None):
        self.send_response(resp.status)
        self.send_header("Content-Type",
                         resp.headers.get("Content-Type", "text/event-stream"))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        ttft = None
        chunks = 0
        content_chars = 0
        reasoning_chars = 0
        stamps = []
        tool_acc = {}
        finish = None
        usage = {}
        client_gone = False

        while True:
            try:
                line = resp.readline()
            except Exception:
                break
            if not line:
                break

            if not client_gone:
                try:
                    self.wfile.write(line)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # The agent hung up. Keep draining so the upstream request
                    # finishes and its usage chunk still gets recorded.
                    client_gone = True

            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if not data or data == b"[DONE]":
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue

            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                text = delta.get("content") or ""
                reasoning = (delta.get("reasoning_content")
                             or delta.get("reasoning") or "")
                if text or reasoning:
                    now = time.monotonic()
                    if ttft is None:
                        ttft = now - started
                    stamps.append(now)
                content_chars += len(text)
                reasoning_chars += len(reasoning)
                for call in delta.get("tool_calls") or []:
                    idx = call.get("index", 0)
                    name, args = tool_acc.get(idx, ("", ""))
                    fn = call.get("function") or {}
                    tool_acc[idx] = (
                        name or fn.get("name") or "",
                        args + (fn.get("arguments") or ""),
                    )
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            chunks += 1

        calls = _collapse_tool_deltas(tool_acc)
        gaps = [stamps[i] - stamps[i - 1] for i in range(1, len(stamps))]
        row.update(
            status=resp.status,
            duration_s=round(time.monotonic() - started, 3),
            ttft_s=round(ttft, 3) if ttft is not None else None,
            n_chunks=chunks,
            content_chars=content_chars,
            reasoning_chars=reasoning_chars,
            mean_itl_s=round(sum(gaps) / len(gaps), 4) if gaps else None,
            tool_calls=[c["name"] for c in calls],
            read_paths=read_paths(calls),
            finish_reason=finish,
            client_disconnected=client_gone,
        )
        self._record_usage(row, usage)
        self._record(row)
        self._finish_span(span, row)

    @staticmethod
    def _record_usage(row, usage):
        row["prompt_tokens"] = usage.get("prompt_tokens")
        row["completion_tokens"] = usage.get("completion_tokens")
        row["total_tokens"] = usage.get("total_tokens")
        details = usage.get("completion_tokens_details") or {}
        row["reasoning_tokens"] = details.get("reasoning_tokens")
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached is None:
            cached = usage.get("cache_read_input_tokens")
        row["cached_prompt_tokens"] = cached


def serve(port, upstream, api_key, recorder, agent, telemetry=None):
    """Start a proxy for one agent. Returns the server; the caller shuts it down."""
    bound = type("BoundHandler", (Handler,), {
        "upstream": upstream,
        "api_key": api_key,
        "recorder": recorder,
        "agent": agent,
        "telemetry": telemetry,
    })
    httpd = ThreadingHTTPServer(("127.0.0.1", port), bound)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
