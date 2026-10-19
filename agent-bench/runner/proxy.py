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
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHAT_PATHS = ("/v1/chat/completions", "/chat/completions")
READ_TOOLS = ("read", "Read", "view", "cat")


class Recorder:
    """Append-only JSONL sink shared by every proxy thread.

    Also keeps, per agent, the set of paths that agent asked to read. The
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
            paths = row.get("read_paths")
            if paths:
                self._reads.setdefault(row.get("agent") or "", set()).update(paths)

    def reads_for(self, agent):
        with self._lock:
            return set(self._reads.get(agent, ()))

    def close(self):
        with self._lock:
            self._fh.close()


def _collapse_tool_deltas(acc):
    """Collapse streamed tool_call deltas into [{name, arguments}]."""
    return [{"name": acc[i][0], "arguments": acc[i][1]} for i in sorted(acc)]


def read_paths(tool_calls):
    """File paths the agent asked to read, for coverage accounting."""
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

    def log_message(self, fmt, *args):
        pass  # quiet: the runner owns stdout

    def do_GET(self):
        self._forward(b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._forward(self.rfile.read(length) if length else b"")

    # -- plumbing ---------------------------------------------------------

    def _upstream_request(self, body):
        url = self.upstream.rstrip("/") + self.path
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
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

        row = {
            "agent": self.agent,
            "t_start": time.time(),
            "path": self.path,
            "model": (payload or {}).get("model"),
            "stream": streaming,
            "n_messages": len(((payload or {}).get("messages")) or []),
            "n_tools_offered": len(((payload or {}).get("tools")) or []),
        }
        started = time.monotonic()

        try:
            resp = urllib.request.urlopen(self._upstream_request(body), timeout=1800)
        except urllib.error.HTTPError as exc:
            detail = exc.read()
            row.update(
                status=exc.code,
                error=detail[:500].decode("utf-8", "replace"),
                duration_s=round(time.monotonic() - started, 3),
            )
            self._record(row)
            self._send_raw(exc.code, detail,
                           exc.headers.get("Content-Type", "application/json"))
            return
        except Exception as exc:  # reset, DNS, timeout
            row.update(
                status=0,
                error="{}: {}".format(type(exc).__name__, exc),
                duration_s=round(time.monotonic() - started, 3),
            )
            self._record(row)
            self._send_raw(502, json.dumps({"error": str(exc)}).encode(),
                           "application/json")
            return

        if streaming:
            self._pump_sse(resp, row, started)
        else:
            self._pump_whole(resp, row, started)

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

    def _pump_whole(self, resp, row, started):
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
        self._send_raw(resp.status, body,
                       resp.headers.get("Content-Type", "application/json"))

    def _pump_sse(self, resp, row, started):
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


def serve(port, upstream, api_key, recorder, agent):
    """Start a proxy for one agent. Returns the server; the caller shuts it down."""
    bound = type("BoundHandler", (Handler,), {
        "upstream": upstream,
        "api_key": api_key,
        "recorder": recorder,
        "agent": agent,
    })
    httpd = ThreadingHTTPServer(("127.0.0.1", port), bound)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
