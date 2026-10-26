"""Measure single-request decode and prefill speed against a vLLM server.

The script runs inside the model container, so it uses only the standard
library. Each case streams one chat completion, records time to first token
and the decode rate after it, and diffs vLLM's speculative-decoding counters
around the request so that drafter changes can be compared by acceptance
length as well as by tokens per second.

Long-context cases prepend a random nonce to the corpus so that no prefix-cache
hit can shorten prefill.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import uuid

CODE_PROMPTS = [
    "Write a complete Python module implementing a thread-safe LRU cache with "
    "a per-entry TTL. Include docstrings and unittest-based tests.",
    "Write a TypeScript Express middleware that implements token-bucket rate "
    "limiting per API key backed by Redis. Include types and comments.",
    "Explain how to implement Dijkstra's algorithm with a binary heap in Go, "
    "then give the complete code with a worked example in main.",
]

THINK_PROMPTS = [
    "Design an algorithm to find the k-th smallest element in the union of two "
    "sorted arrays in O(log k) time. Explain it, then give Python code.",
    "Prove that the sum of the first n odd numbers is n squared, then write a "
    "Python function that checks the identity for every n up to 1000.",
]

LONG_QUESTION = (
    "Above is the source of a repository. List every Docker Compose file it "
    "mentions and the model each one serves, as a Markdown table, then explain "
    "in a few paragraphs how the LiteLLM gateway is configured."
)

SPEC_COUNTERS = {
    "drafts": "vllm:spec_decode_num_drafts_total",
    "draft_tokens": "vllm:spec_decode_num_draft_tokens_total",
    "accepted": "vllm:spec_decode_num_accepted_tokens_total",
}


def read_spec_counters(host: str, port: int) -> dict[str, float]:
    conn = http.client.HTTPConnection(host, port, timeout=30)
    conn.request("GET", "/metrics")
    text = conn.getresponse().read().decode()
    conn.close()
    values = {key: 0.0 for key in SPEC_COUNTERS}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        for key, name in SPEC_COUNTERS.items():
            if line.startswith(name + "{") or line.startswith(name + " "):
                values[key] += float(line.rsplit(" ", 1)[1])
    return values


def stream_chat(host: str, port: int, model: str, body: dict) -> dict:
    payload = dict(body, model=model, stream=True,
                   stream_options={"include_usage": True})
    conn = http.client.HTTPConnection(host, port, timeout=3600)
    start = time.perf_counter()
    conn.request("POST", "/v1/chat/completions", json.dumps(payload),
                 {"Content-Type": "application/json"})
    resp = conn.getresponse()
    if resp.status != 200:
        raise RuntimeError(f"HTTP {resp.status}: {resp.read()[:500]!r}")
    first = None
    usage = None
    finish = None
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            break
        chunk = json.loads(data)
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if first is None and any(
                    delta.get(k) for k in ("content", "reasoning_content",
                                           "reasoning")):
                first = time.perf_counter()
            finish = choice.get("finish_reason") or finish
    end = time.perf_counter()
    conn.close()
    return {"start": start, "first": first, "end": end, "usage": usage,
            "finish_reason": finish}


def build_cases(corpus: str | None, long_tokens: list[int]) -> list[dict]:
    off = {"enable_thinking": False}
    cases = []
    for i, prompt in enumerate(CODE_PROMPTS, 1):
        cases.append({
            "id": f"code-{i}", "kind": "code",
            "body": {"messages": [{"role": "user", "content": prompt}],
                     "max_tokens": 512, "temperature": 0,
                     "chat_template_kwargs": off},
        })
    for i, prompt in enumerate(THINK_PROMPTS, 1):
        cases.append({
            "id": f"think-{i}", "kind": "think",
            "body": {"messages": [{"role": "user", "content": prompt}],
                     "max_tokens": 1536, "temperature": 1.0, "top_p": 0.95,
                     "top_k": 20, "seed": 1234},
        })
    if corpus:
        for tokens in long_tokens:
            # About 3.2 characters per token for this mixed code/prose corpus.
            text = corpus[: int(tokens * 3.2)]
            nonce = uuid.uuid4().hex
            content = f"Request {nonce}.\n\n{text}\n\n{LONG_QUESTION}"
            cases.append({
                "id": f"long-{tokens // 1000}k", "kind": "long",
                "body": {"messages": [{"role": "user", "content": content}],
                         "max_tokens": 384, "temperature": 0,
                         "chat_template_kwargs": off},
            })
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--model", default="qwen3.8-27b")
    parser.add_argument("--corpus", help="text file used for long prompts")
    parser.add_argument("--long-tokens", default="32000,100000",
                        help="comma-separated approximate long prompt sizes")
    parser.add_argument("--label", default="run")
    parser.add_argument("--only", help="comma-separated case kinds to run")
    args = parser.parse_args()

    corpus = None
    if args.corpus:
        with open(args.corpus, encoding="utf-8") as f:
            corpus = f.read()
    long_tokens = [int(x) for x in args.long_tokens.split(",") if x]
    cases = build_cases(corpus, long_tokens)
    if args.only:
        kinds = set(args.only.split(","))
        cases = [c for c in cases if c["kind"] in kinds]

    stream_chat(args.host, args.port, args.model, {
        "messages": [{"role": "user", "content": "Say hi."}],
        "max_tokens": 16, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False}})

    results = []
    for case in cases:
        before = read_spec_counters(args.host, args.port)
        r = stream_chat(args.host, args.port, args.model, case["body"])
        after = read_spec_counters(args.host, args.port)
        spec = {k: after[k] - before[k] for k in SPEC_COUNTERS}
        usage = r["usage"] or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        ttft = (r["first"] or r["end"]) - r["start"]
        decode_s = r["end"] - (r["first"] or r["end"])
        row = {
            "label": args.label, "id": case["id"], "kind": case["kind"],
            "prompt_tokens": prompt_tokens, "completion_tokens": completion,
            "finish_reason": r["finish_reason"],
            "ttft_s": round(ttft, 3), "total_s": round(r["end"] - r["start"], 3),
            "decode_tps": round((completion - 1) / decode_s, 2)
            if decode_s > 0 and completion > 1 else None,
            "prefill_tps": round(prompt_tokens / ttft, 1) if ttft > 0 else None,
            "drafts": spec["drafts"], "draft_tokens": spec["draft_tokens"],
            "accepted": spec["accepted"],
            "acceptance_length": round(1 + spec["accepted"] / spec["drafts"], 3)
            if spec["drafts"] else None,
        }
        results.append(row)
        print(f"{row['id']:9s} prompt={prompt_tokens:>7} out={completion:>5} "
              f"ttft={row['ttft_s']:>8.2f}s decode={row['decode_tps']} tok/s "
              f"prefill={row['prefill_tps']} tok/s "
              f"accept_len={row['acceptance_length']}", file=sys.stderr)

    print(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
