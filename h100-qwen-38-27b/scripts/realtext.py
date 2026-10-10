#!/usr/bin/env python3
"""Real-text decode throughput against vLLM directly (stdlib only), on the VM.

  python3 scripts/realtext.py 1 32      # one run per concurrency level

Each request is a natural-language prompt with thinking off and exactly 512
output tokens (ignore_eos), so speculative decoding (MTP) sees real text,
unlike `vllm bench serve` random prompts. Prints aggregate and per-stream
output tok/s per concurrency level.
"""
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

URL = "http://127.0.0.1:8096/v1/chat/completions"
OUT = 512
TOPICS = [
    "how TCP congestion control works", "the history of the printing press",
    "how a compiler optimizes loops", "photosynthesis in C4 plants",
    "the causes of the 2008 financial crisis", "how GPS determines position",
    "garbage collection in the JVM", "the water cycle", "how vaccines train immunity",
    "B-tree indexes in databases", "the French Revolution", "how transformers use attention",
    "plate tectonics", "public-key cryptography", "the life cycle of stars",
    "how HTTP caching works", "supply and demand", "how Git stores history",
    "the Roman road network", "how lithium-ion batteries age",
]


def one(i):
    body = {
        "model": "qwen3.8-27b",
        "messages": [{"role": "user", "content":
                      f"Write a detailed technical explanation of {TOPICS[i % len(TOPICS)]}."}],
        "max_tokens": OUT, "min_tokens": OUT, "ignore_eos": True, "temperature": 0.7,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["usage"]["completion_tokens"]


for c in map(int, sys.argv[1:] or ["1", "32"]):
    n = max(4, c * 2)
    t0 = time.time()
    with ThreadPoolExecutor(c) as ex:
        toks = sum(ex.map(one, range(n)))
    wall = time.time() - t0
    # Requests run in waves of c, so per-stream speed = aggregate / c.
    print(f"realtext   c={c:<3} n={n:<3} out={toks / wall:7.1f} tok/s  per-stream={toks / wall / c:6.1f}")
