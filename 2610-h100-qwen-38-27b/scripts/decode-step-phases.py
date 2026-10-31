#!/usr/bin/env python3
"""Break vLLM decode steps into phases from a torch profiler trace.

  python3 scripts/decode-step-phases.py /data/vllm-cache/profile [--top 12]

Point it at the directory --profiler-config.torch_profiler_dir wrote (the
newest rank0 trace there is read). Each step runs from one execute_model
annotation to the next. Two views of the same step:

- GPU side (CUPTI kernel events, present on Linux): the union of kernel and
  copy intervals per step is the time the GPU was busy; the rest is idle,
  i.e. the engine's per-step overhead that async scheduling and Model Runner
  V2 are meant to hide. --top lists the kernels by total time, which shows
  what a step spends its bytes on (GEMMs, attention, the Gated DeltaNet
  recurrence, the drafter, sampling).
- CPU side (CUDA runtime calls on the engine thread): how long the thread
  blocked in stream synchronizations and device-to-host copies, and how many
  kernel and graph launches a step takes. This view also works where CUPTI
  recorded no kernels (WSL 2, for example).

Stdlib only.
"""

import argparse
import collections
import glob
import gzip
import json
import os
import statistics


def ms(us: float) -> float:
    return us / 1e3


def union_length(intervals: list[tuple[float, float]]) -> float:
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trace_dir")
    parser.add_argument("--top", type=int, default=12, help="kernels to list by total GPU time")
    args = parser.parse_args()

    paths = sorted(glob.glob(os.path.join(args.trace_dir, "*rank0*.pt.trace.json*")), key=os.path.getmtime)
    if not paths:
        raise SystemExit(f"no rank0 trace under {args.trace_dir}")
    path = paths[-1]
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path) as f:
        events = json.load(f)["traceEvents"]
    print(f"trace: {os.path.basename(path)}")

    runtime = sorted((e for e in events if e.get("cat") in ("cuda_runtime", "cuda_driver") and "dur" in e),
                     key=lambda e: e["ts"])
    gpu = sorted((e for e in events if e.get("cat") in ("kernel", "gpu_memcpy", "gpu_memset") and "dur" in e),
                 key=lambda e: e["ts"])
    ann = [e for e in events if e.get("cat") == "user_annotation" and "dur" in e]
    steps_ann = [e for e in ann if "execute_model" in e.get("name", "")] or ann
    starts = sorted(e["ts"] for e in steps_ann)
    if len(starts) < 3:
        raise SystemExit("fewer than three step annotations in the trace")

    rows = []
    kernel_time = collections.Counter()
    kernel_count = collections.Counter()
    for s0, s1 in zip(starts, starts[1:]):
        step = [e for e in runtime if s0 <= e["ts"] < s1]
        gstep = [e for e in gpu if s0 <= e["ts"] < s1]
        syncs = [e for e in step if e["name"] in ("cudaStreamSynchronize", "cudaDeviceSynchronize", "cudaEventSynchronize")]
        copies = [e for e in step if e["name"] in ("cudaMemcpyAsync", "cudaMemcpy")]
        # A device-to-host copy that waits on the GPU shows as one long cudaMemcpyAsync.
        block = max((e["dur"] for e in copies), default=0.0)
        row = {
            "period": s1 - s0,
            "sync_total": sum(e["dur"] for e in syncs),
            "n_syncs": len(syncs),
            "n_copies": len(copies),
            "block_copy": block,
            "launches": sum(1 for e in step if "Launch" in e["name"] and "Graph" not in e["name"]),
            "graph_launches": sum(1 for e in step if e["name"] == "cudaGraphLaunch"),
            "gpu_busy": union_length([(e["ts"], e["ts"] + e["dur"]) for e in gstep]),
            "n_kernels": len(gstep),
        }
        rows.append(row)
        for e in gstep:
            if e.get("cat") == "kernel":
                kernel_time[e["name"]] += e["dur"]
                kernel_count[e["name"]] += 1

    # Drop the first and last step: partially covered by the window.
    rows = rows[1:-1] if len(rows) > 4 else rows
    med = lambda k: statistics.median(r[k] for r in rows)  # noqa: E731
    period = med("period")
    print(f"{len(rows)} steps, median period {ms(period):.2f} ms ({1e6 / period:.1f} steps/s)")
    if any(r["n_kernels"] for r in rows):
        busy = med("gpu_busy")
        print(f"  GPU busy (kernels + copies)       {ms(busy):6.2f} ms  = {100 * busy / period:.0f}% of the step")
        print(f"  GPU idle                          {ms(period - busy):6.2f} ms")
        print(f"  kernels + copies per step         {med('n_kernels'):6.0f}")
    else:
        print("  no GPU kernel events in the trace (no CUPTI); CPU-side view only")
    print(f"  engine-thread kernel launches     {med('launches'):6.0f}   CUDA graph launches {med('graph_launches'):.0f}")
    print(f"  stream/device synchronizations    {med('n_syncs'):6.0f}   blocked {ms(med('sync_total')):.2f} ms in total")
    print(f"  memcpy calls                      {med('n_copies'):6.0f}   longest (device-to-host wait) {ms(med('block_copy')):.2f} ms")

    if kernel_time and args.top:
        total = sum(kernel_time.values())
        n = len(rows)
        print(f"\ntop kernels by GPU time over {n} steps (ms per step, share of kernel time, launches per step):")
        for name, t in kernel_time.most_common(args.top):
            short = name if len(name) <= 90 else name[:87] + "..."
            print(f"  {ms(t) / n:7.3f} ms  {100 * t / total:5.1f}%  x{kernel_count[name] / n:5.1f}  {short}")


if __name__ == "__main__":
    main()
