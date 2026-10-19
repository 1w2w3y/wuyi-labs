"""Model compatibility gate.

A coding agent needs several things from an endpoint, and a model can fail any
of them while looking healthy to a casual curl. A run against such a model
still "succeeds": the agents do no work and the throughput number means
nothing. So before any traffic we check, against the gateway directly:

  1. the model is listed on /v1/models
  2. a non-streaming completion returns assistant text
  3. a streaming completion delivers non-empty `content` deltas, not
     reasoning alone
  4. a tool definition comes back as a tool call
  5. streaming and tools together behave -- the actual agent shape, and the
     combination gateways most often get wrong

Check 3 earns its place on reasoning models: with a small token budget,
reasoning can consume all of it and the reply comes back with empty content
and `finish_reason: length`. Nothing downstream reports that as an error.

Failures print what broke and what to do about it, and exit non-zero.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

TOOL = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "List the files of a directory",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


def _post(base_url, body, api_key, timeout=180, stream=False):
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": "Bearer " + api_key} if api_key else {}),
        },
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp if stream else json.loads(resp.read())


def check_listed(base_url, model, api_key):
    req = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": "Bearer " + api_key} if api_key else {},
    )
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception as exc:
        return False, "cannot reach {}/models: {}".format(base_url.rstrip('/'), exc)
    ids = [m.get("id") for m in data.get("data") or []]
    if model not in ids:
        near = [i for i in ids if model.lower() in str(i).lower()]
        hint = "  close matches: " + ", ".join(near) if near else \
               "  {} models listed".format(len(ids))
        return False, "model {!r} is not served here\n{}".format(model, hint)
    return True, "listed among {} models".format(len(ids))


def check_nonstream(base_url, model, api_key, max_tokens):
    body = {
        "model": model,
        "messages": [{"role": "user", "content":
                      "Reply with exactly: READY"}],
        "max_tokens": max_tokens,
    }
    try:
        data = _post(base_url, body, api_key)
    except urllib.error.HTTPError as exc:
        return False, "HTTP {}: {}".format(exc.code,
                                           exc.read()[:300].decode("utf-8", "replace"))
    except Exception as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)

    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    if not text.strip():
        return False, (
            "returned empty content (finish_reason={}). If this is a reasoning "
            "model, raise max_tokens: reasoning can consume the whole budget "
            "before any content is emitted.".format(choice.get("finish_reason"))
        )
    return True, "{!r}".format(text.strip()[:40])


def check_stream(base_url, model, api_key, max_tokens):
    body = {
        "model": model,
        "messages": [{"role": "user", "content":
                      "Count from 1 to 10, separated by spaces."}],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    try:
        resp = _post(base_url, body, api_key, stream=True)
    except Exception as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)

    content = ""
    reasoning = ""
    finish = None
    ttft = None
    started = time.monotonic()
    while True:
        line = resp.readline()
        if not line:
            break
        if not line.startswith(b"data:"):
            continue
        blob = line[5:].strip()
        if not blob or blob == b"[DONE]":
            continue
        try:
            event = json.loads(blob)
        except ValueError:
            continue
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                if ttft is None:
                    ttft = time.monotonic() - started
                content += delta["content"]
            reasoning += (delta.get("reasoning_content")
                          or delta.get("reasoning") or "")
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]

    if not content.strip() and reasoning.strip():
        return False, (
            "streamed {} characters of reasoning_content and no content "
            "(finish_reason={}).\n"
            "  The agent sees an empty reply and ends its turn having done "
            "nothing. If finish_reason is 'length', reasoning consumed the "
            "whole budget: raise max_tokens well above the reasoning length. "
            "Otherwise turn reasoning off upstream or pick another model."
            .format(len(reasoning), finish)
        )
    if not content.strip():
        return False, "streamed no content at all (finish_reason={})".format(finish)
    return True, "{} chars, first token {:.2f}s".format(
        len(content), ttft if ttft is not None else float("nan"))


def check_tools(base_url, model, api_key, max_tokens):
    body = {
        "model": model,
        "messages": [{"role": "user",
                      "content": "List the files in the current folder."}],
        "tools": [TOOL],
        "max_tokens": max_tokens,
    }
    try:
        data = _post(base_url, body, api_key)
    except Exception as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)

    message = ((data.get("choices") or [{}])[0].get("message")) or {}
    calls = message.get("tool_calls") or []
    if not calls:
        return False, (
            "did not call the offered tool. A coding agent cannot read files "
            "without tool calling, so every session would be a single empty "
            "turn."
        )
    names = [(c.get("function") or {}).get("name") for c in calls]
    return True, "called {}".format(", ".join(n for n in names if n))


def check_stream_tools(base_url, model, api_key, max_tokens):
    """Streaming plus tools: what every agent turn actually looks like."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content":
                      "List the files in the current folder, then say what you found."}],
        "tools": [TOOL],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    try:
        resp = _post(base_url, body, api_key, stream=True)
    except Exception as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)

    calls = {}
    content = ""
    finish = None
    while True:
        line = resp.readline()
        if not line:
            break
        if not line.startswith(b"data:"):
            continue
        blob = line[5:].strip()
        if not blob or blob == b"[DONE]":
            continue
        try:
            event = json.loads(blob)
        except ValueError:
            continue
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            content += delta.get("content") or ""
            for call in delta.get("tool_calls") or []:
                idx = call.get("index", 0)
                name, args = calls.get(idx, ("", ""))
                fn = call.get("function") or {}
                calls[idx] = (name or fn.get("name") or "",
                              args + (fn.get("arguments") or ""))
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]

    if not calls and not content.strip():
        return False, (
            "streamed neither a tool call nor content (finish_reason={}). "
            "Streamed tool calling is broken on this route; the agent will "
            "sit in empty turns.".format(finish)
        )
    if not calls:
        return False, (
            "streamed content but never called the tool, so the agent cannot "
            "reach the corpus (finish_reason={})".format(finish)
        )
    for idx, (name, args) in calls.items():
        try:
            json.loads(args or "{}")
        except ValueError:
            return False, (
                "tool call {!r} streamed arguments that are not valid JSON: "
                "{!r}. Fragmented argument deltas are not being reassembled "
                "correctly upstream.".format(name, args[:120])
            )
    return True, "called {} with valid JSON arguments".format(
        ", ".join(n for n, _ in calls.values() if n))


def run(base_url, model, api_key="", max_tokens=512, verbose=True):
    """Returns (ok, [(name, ok, detail), ...])."""
    checks = [
        ("model listed", check_listed, False),
        ("non-streaming content", check_nonstream, True),
        ("streaming content", check_stream, True),
        ("tool calling", check_tools, True),
        ("streaming + tools", check_stream_tools, True),
    ]
    results = []
    ok_all = True
    for name, fn, needs_tokens in checks:
        if needs_tokens:
            ok, detail = fn(base_url, model, api_key, max_tokens)
        else:
            ok, detail = fn(base_url, model, api_key)
        results.append((name, ok, detail))
        if verbose:
            print("  [{}] {:<24} {}".format("ok" if ok else "FAIL", name, detail),
                  flush=True)
        if not ok:
            ok_all = False
            break  # later checks are meaningless once one fails
    return ok_all, results


def main(argv):
    if len(argv) < 3:
        print("usage: preflight.py <base-url-with-/v1> <model> [api-key] [max-tokens]")
        return 2
    base_url, model = argv[1], argv[2]
    api_key = argv[3] if len(argv) > 3 else ""
    max_tokens = int(argv[4]) if len(argv) > 4 else 512
    print("preflight: {} on {}".format(model, base_url))
    ok, _ = run(base_url, model, api_key, max_tokens)
    print("preflight: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
