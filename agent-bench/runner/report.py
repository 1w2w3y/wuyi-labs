"""Turn a run's request log into the numbers worth quoting.

Two groups, and the distinction matters:

  Server-side load  -- what the serving stack delivered: output tokens per
  second, prompt tokens per second, TTFT and per-stream rate under the
  concurrency the agents actually produced. This is the throughput figure.

  Agent productivity -- whether the agents did any work for it: files
  reviewed, tokens per reviewed file, how much of the queue was abandoned. A
  high token rate with low coverage means the model is busy talking to itself.

Quoting the first without the second is how a benchmark flatters a model that
loops. Both go in the summary.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PROM = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9eE+.\-]+)$")

# vLLM counters worth a before/after delta.
VLLM_KEYS = (
    "vllm:generation_tokens_total",
    "vllm:prompt_tokens_total",
    "vllm:prefix_cache_hits_total",
    "vllm:prefix_cache_queries_total",
    "vllm:num_preemptions_total",
    "vllm:request_success_total",
)


def _pct(values, q):
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return round(s[0], 3)
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return round(s[lo] * (1 - frac) + s[hi] * frac, 3)


def _sum_prom(text, name):
    """Sum a counter across all label sets."""
    if not text:
        return None
    total = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith(name):
            continue
        m = PROM.match(line)
        if not m or m.group(1) != name:
            continue
        try:
            total = (total or 0.0) + float(m.group(3))
        except ValueError:
            continue
    return total


def _server_delta(out_dir):
    before = _read(out_dir / "vllm-before.prom")
    after = _read(out_dir / "vllm-after.prom")
    if not before or not after:
        return None
    delta = {}
    for key in VLLM_KEYS:
        b, a = _sum_prom(before, key), _sum_prom(after, key)
        if b is None or a is None:
            continue
        delta[key] = round(a - b, 2)
    hits = delta.get("vllm:prefix_cache_hits_total")
    queries = delta.get("vllm:prefix_cache_queries_total")
    if hits is not None and queries:
        delta["prefix_cache_hit_rate"] = round(hits / queries, 4)
    return delta or None


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _load_requests(out_dir):
    rows = []
    path = Path(out_dir) / "requests.jsonl"
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _concurrency_profile(rows):
    """Mean in-flight requests, by sweeping request start/end events."""
    events = []
    for r in rows:
        start = r.get("t_start")
        dur = r.get("duration_s")
        if start is None or dur is None:
            continue
        events.append((start, 1))
        events.append((start + dur, -1))
    if not events:
        return None, None
    events.sort()
    inflight = 0
    peak = 0
    weighted = 0.0
    prev = events[0][0]
    for t, delta in events:
        weighted += inflight * (t - prev)
        prev = t
        inflight += delta
        peak = max(peak, inflight)
    span = events[-1][0] - events[0][0]
    return (round(weighted / span, 2) if span > 0 else None), peak


def build(out_dir, meta, files, scenario):
    rows = _load_requests(out_dir)
    ok = [r for r in rows if r.get("status") == 200]
    failed = [r for r in rows if r.get("status") != 200]

    out_tokens = sum(r.get("completion_tokens") or 0 for r in ok)
    in_tokens = sum(r.get("prompt_tokens") or 0 for r in ok)
    cached = sum(r.get("cached_prompt_tokens") or 0 for r in ok)
    reasoning = sum(r.get("reasoning_tokens") or 0 for r in ok)

    elapsed = meta.get("elapsed_seconds") or 0
    ttfts = [r["ttft_s"] for r in ok if r.get("ttft_s") is not None]
    prompts = [r["prompt_tokens"] for r in ok if r.get("prompt_tokens")]

    # Per-stream output rate: a request's own tokens over its own decode time.
    per_stream = []
    for r in ok:
        comp = r.get("completion_tokens")
        dur = r.get("duration_s")
        ttft = r.get("ttft_s")
        if not comp or not dur:
            continue
        decode = dur - (ttft or 0)
        if decode > 0.05:
            per_stream.append(comp / decode)

    mean_conc, peak_conc = _concurrency_profile(ok)

    # Coverage from the wire, not from what the agents claimed.
    queue = [f for f, _ in files]
    reviewed = set()
    for r in ok:
        for raw in r.get("read_paths") or []:
            p = raw.replace("\\", "/").lstrip("./")
            for f in queue:
                if p == f or p.endswith(f):
                    reviewed.add(f)
                    break
    abandoned = sorted(set(queue) - reviewed)

    sessions = [s for a in meta.get("agent_sessions") or []
                for s in a.get("sessions") or []]
    timed_out = sum(1 for s in sessions if s.get("timed_out"))
    redriven = sum(1 for s in sessions if s.get("attempt", 1) > 1)

    floor = scenario.get("interactivity_floor_tps")
    p90_stream = _pct(per_stream, 0.10)  # the slow end: 90% of streams beat it

    summary = {
        "scenario": meta.get("scenario", {}).get("name"),
        "model": meta.get("model"),
        "repo_sha": meta.get("repo_sha"),
        "agents": meta.get("agents"),
        "elapsed_seconds": elapsed,
        "load": {
            "requests_ok": len(ok),
            "requests_failed": len(failed),
            "output_tokens": out_tokens,
            "prompt_tokens": in_tokens,
            "cached_prompt_tokens": cached or None,
            "cached_prompt_fraction": (round(cached / in_tokens, 4)
                                       if cached and in_tokens else None),
            "reasoning_tokens": reasoning or None,
            "output_tokens_per_second": (round(out_tokens / elapsed, 1)
                                         if elapsed else None),
            "prompt_tokens_per_second": (round(in_tokens / elapsed, 1)
                                         if elapsed else None),
            "mean_concurrent_requests": mean_conc,
            "peak_concurrent_requests": peak_conc,
            "ttft_p50_s": _pct(ttfts, 0.50),
            "ttft_p95_s": _pct(ttfts, 0.95),
            "per_stream_tps_p50": _pct(per_stream, 0.50),
            "per_stream_tps_p90_slow": p90_stream,
            "prompt_tokens_p50": _pct(prompts, 0.50),
            "prompt_tokens_p95": _pct(prompts, 0.95),
            "interactivity_floor_tps": floor,
            "meets_interactivity_floor": (None if floor is None or p90_stream is None
                                          else p90_stream >= floor),
        },
        "productivity": {
            "queue_files": len(queue),
            "files_reviewed": len(reviewed),
            "coverage": round(len(reviewed) / len(queue), 4) if queue else None,
            "files_abandoned": len(abandoned),
            "sessions_run": len(sessions),
            "sessions_timed_out": timed_out,
            "sessions_redriven": redriven,
            "output_tokens_per_reviewed_file": (round(out_tokens / len(reviewed), 1)
                                                if reviewed else None),
            "prompt_tokens_per_reviewed_file": (round(in_tokens / len(reviewed), 1)
                                                if reviewed else None),
        },
        "server": _server_delta(out_dir),
        "abandoned_sample": abandoned[:20],
        "failures_sample": [
            {"status": r.get("status"), "error": (r.get("error") or "")[:200]}
            for r in failed[:5]
        ],
    }
    return summary


def render(s):
    load = s["load"]
    prod = s["productivity"]
    lines = []
    add = lines.append

    add("=" * 68)
    add("agent-bench  {}  model={}  agents={}".format(
        s.get("scenario"), s.get("model"), s.get("agents")))
    add("elapsed {}s".format(s.get("elapsed_seconds")))
    add("=" * 68)

    add("")
    add("Server load")
    add("  output            {} tok/s  ({} tokens)".format(
        load["output_tokens_per_second"], load["output_tokens"]))
    add("  prompt            {} tok/s  ({} tokens)".format(
        load["prompt_tokens_per_second"], load["prompt_tokens"]))
    if load.get("cached_prompt_fraction") is not None:
        add("  prompt cached     {:.1%} of input".format(load["cached_prompt_fraction"]))
    if load.get("reasoning_tokens"):
        add("  reasoning         {} tokens of the output".format(load["reasoning_tokens"]))
    add("  requests          {} ok, {} failed".format(
        load["requests_ok"], load["requests_failed"]))
    add("  concurrency       {} mean, {} peak in flight".format(
        load["mean_concurrent_requests"], load["peak_concurrent_requests"]))
    add("  TTFT              p50 {}s   p95 {}s".format(
        load["ttft_p50_s"], load["ttft_p95_s"]))
    add("  per-stream rate   p50 {} tok/s   slowest decile {} tok/s".format(
        load["per_stream_tps_p50"], load["per_stream_tps_p90_slow"]))
    add("  prompt size       p50 {} tok   p95 {} tok".format(
        load["prompt_tokens_p50"], load["prompt_tokens_p95"]))
    if load.get("meets_interactivity_floor") is not None:
        verdict = "yes" if load["meets_interactivity_floor"] else "NO"
        add("  usable to code against at {} tok/s floor: {}".format(
            load["interactivity_floor_tps"], verdict))

    add("")
    add("Agent productivity")
    add("  files reviewed    {} of {} ({:.1%})".format(
        prod["files_reviewed"], prod["queue_files"], prod["coverage"] or 0))
    add("  abandoned         {} files".format(prod["files_abandoned"]))
    add("  sessions          {} run, {} re-driven, {} timed out".format(
        prod["sessions_run"], prod["sessions_redriven"], prod["sessions_timed_out"]))
    add("  cost per file     {} output tok, {} prompt tok".format(
        prod["output_tokens_per_reviewed_file"],
        prod["prompt_tokens_per_reviewed_file"]))

    if s.get("server"):
        add("")
        add("Server counters (delta over the run)")
        for key, val in s["server"].items():
            add("  {:<34} {}".format(key, val))

    if s.get("failures_sample"):
        add("")
        add("Failures")
        for f in s["failures_sample"]:
            add("  {} {}".format(f["status"], f["error"]))

    add("")
    return "\n".join(lines)


def main(argv):
    if len(argv) < 2:
        print("usage: report.py <result-dir>")
        return 2
    out_dir = Path(argv[1])
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    scenario = meta.get("scenario") or {}
    files = [(f, 0) for f in _queue_from_meta(meta)]
    summary = build(out_dir, meta, files, scenario)
    print(render(summary))
    return 0


def _queue_from_meta(meta):
    out = []
    for agent in meta.get("agent_sessions") or []:
        for session in agent.get("sessions") or []:
            out.extend(session.get("files") or [])
    seen = set()
    return [f for f in out if not (f in seen or seen.add(f))]


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
