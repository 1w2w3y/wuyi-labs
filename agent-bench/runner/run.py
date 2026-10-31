"""agent-bench orchestrator.

Loads a scenario, builds a deterministic work queue from the corpus, runs N
opencode agents against it concurrently through the metering proxy, and writes
a result bundle.

The queue is the point. Asking an agent to "review every file and don't stop"
measures the model's willingness to keep going, not the server: a model that
quits after three files produces less load, and two models are never
comparable. Here the harness enumerates and shards the files, hands each agent
one batch at a time, and checks from the wire which files were actually read.
Total work is fixed by the corpus, so the number means the same thing for every
model.

Sessions are per batch rather than one long session per agent. opencode's
`--continue` resumes "the last session", which is ambiguous with several agents
running at once, and there is no stable way to recover a session id from the
CLI. Batch size therefore sets how far a session's context grows.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import proxy as proxy_mod  # noqa: E402
import report as report_mod  # noqa: E402
import preflight as preflight_mod  # noqa: E402
import telemetry as telemetry_mod  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is missing: apt-get install python3-yaml, or pip install pyyaml")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


# -- scenario and corpus -------------------------------------------------


def load_scenario(name):
    path = Path(name)
    if not path.exists():
        path = ROOT / "scenarios" / name
        if not path.suffix:
            path = path.with_suffix(".yaml")
    if not path.exists():
        available = sorted(p.stem for p in (ROOT / "scenarios").glob("*.yaml"))
        sys.exit("no scenario {!r}; available: {}".format(name, ", ".join(available)))
    with open(path, encoding="utf-8") as fh:
        scenario = yaml.safe_load(fh)
    scenario.setdefault("name", path.stem)
    return scenario, path


def resolve_repo(scenario, corpus_dir):
    repo = scenario["repo"]
    if os.path.isabs(repo):
        path = Path(repo)
    else:
        path = Path(corpus_dir) / repo
    if not path.is_dir():
        sys.exit(
            "corpus repo not found: {}\n"
            "Run corpus/fetch.sh, or mount a repo and set 'repo' to its "
            "absolute path.".format(path)
        )
    sha_file = path / ".sha"
    sha = sha_file.read_text().strip() if sha_file.exists() else None
    if sha is None:
        try:
            sha = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip() or None
        except Exception:
            sha = None
    return path, sha


def enumerate_files(repo, scenario):
    """The work queue: deterministic, sorted, filtered by size."""
    include = scenario.get("paths") or ["**/*"]
    exclude = scenario.get("exclude") or []
    lo = int(scenario.get("min_file_bytes") or 0)
    hi = int(scenario.get("max_file_bytes") or 0)

    seen = {}
    for pattern in include:
        for path in repo.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(repo).as_posix()
            if rel in seen:
                continue
            if any(path.match(pat) or _match_posix(rel, pat) for pat in exclude):
                continue
            size = path.stat().st_size
            if size < lo or (hi and size > hi):
                continue
            seen[rel] = size
    return sorted(seen.items())


def _match_posix(rel, pattern):
    """Path.match does not handle leading '**/', so check the posix form too."""
    from fnmatch import fnmatch
    if pattern.startswith("**/"):
        return fnmatch(rel, pattern[3:]) or fnmatch(rel, pattern)
    return fnmatch(rel, pattern)


def shard(files, n_agents):
    """Contiguous slices, so each agent walks one region of the tree.

    Contiguous rather than round-robin on purpose: neighbouring files share
    imports and headers, so a session's growing prompt reuses its own prefix,
    which is what the server's prefix cache sees in real agent traffic.
    """
    if n_agents <= 0:
        return []
    per = len(files) // n_agents
    extra = len(files) % n_agents
    out = []
    start = 0
    for i in range(n_agents):
        take = per + (1 if i < extra else 0)
        out.append(files[start:start + take])
        start += take
    return out


def batches(items, size):
    size = max(1, int(size))
    return [items[i:i + size] for i in range(0, len(items), size)]


# -- agent ---------------------------------------------------------------


class Agent(threading.Thread):
    def __init__(self, idx, slice_files, scenario, cfg_path, repo, out_dir,
                 goal_template, deadline, stop_event, opencode_bin, recorder,
                 state_root, telemetry=None, run_span=None, run_id=""):
        super().__init__(daemon=True)
        self.idx = idx
        self.label = "agent-{:02d}".format(idx)
        self.scenario = scenario
        self.cfg_path = cfg_path
        self.repo = repo
        self.out_dir = out_dir
        self.goal_template = goal_template
        self.deadline = deadline
        self.stop_event = stop_event
        self.opencode_bin = opencode_bin
        self.recorder = recorder
        self.state_root = state_root
        self.telemetry = telemetry
        self.run_span = run_span
        self.run_id = run_id
        self.batches = batches([f for f, _ in slice_files],
                               scenario.get("batch_size", 6))
        self.assigned = [f for f, _ in slice_files]
        self.sessions = []
        self.log_path = out_dir / "logs" / (self.label + ".log")

    def prompt_for(self, files):
        listing = "\n".join("- " + f for f in files)
        return self.goal_template.replace("$ARGUMENTS", listing)

    def _unread(self, files, before):
        """Which of these files the agent never actually read.

        Coverage comes from the read tool calls the proxy saw, not from the
        agent's own account of itself. A model that reports findings for a file
        it never opened counts as not having reviewed it.
        """
        new = self.recorder.reads_for(self.label) - before
        touched = set()
        for raw in new:
            p = raw.replace("\\", "/")
            try:
                rel = Path(raw).resolve().relative_to(self.repo.resolve()).as_posix()
                touched.add(rel)
                continue
            except (ValueError, OSError):
                pass
            touched.add(p.lstrip("./"))
        missed = []
        for f in files:
            if f in touched or any(t.endswith(f) or f.endswith(t) for t in touched):
                continue
            missed.append(f)
        return missed

    def run(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        retries = int(self.scenario.get("retries_per_batch") or 0)
        with open(self.log_path, "a", encoding="utf-8") as log:
            for n, batch in enumerate(self.batches, 1):
                pending = list(batch)
                for attempt in range(retries + 1):
                    if self.stop_event.is_set() or time.time() > self.deadline:
                        log.write("\n=== stopping: budget reached ===\n")
                        return
                    rec = self._one_session(pending, n, attempt, log)
                    self.sessions.append(rec)
                    if not rec["missed"]:
                        break
                    pending = rec["missed"]
                    if attempt < retries:
                        log.write("\n=== re-driving {} unread file(s) ===\n"
                                  .format(len(pending)))

    def _one_session(self, files, batch_no, attempt, log):
        env = dict(os.environ)
        env["OPENCODE_CONFIG_CONTENT"] = self.cfg_path.read_text(encoding="utf-8")
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        # opencode takes its directory from PWD ahead of the process's real
        # cwd, and the entrypoint's `cd` leaves PWD at /opt/agent-bench. From
        # there the corpus is an external directory, so every read is denied.
        env["PWD"] = str(self.repo)
        # Give every agent its own opencode state, so concurrent sessions do
        # not contend on one store.
        state = self.state_root / self.label
        for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"),
                         ("XDG_STATE_HOME", "state"), ("XDG_CONFIG_HOME", "config")):
            d = state / sub
            d.mkdir(parents=True, exist_ok=True)
            env[var] = str(d)

        model = "bench/" + self.scenario["_model"]
        cmd = [self.opencode_bin, "run", "--pure", "-m", model,
               "--title", "{} b{}".format(self.label, batch_no),
               self.prompt_for(files)]

        header = "\n{} batch {} attempt {} ({} files) {}\n".format(
            "=" * 8, batch_no, attempt + 1, len(files),
            datetime.now(timezone.utc).strftime("%H:%M:%S"))
        log.write(header)
        log.flush()

        started = time.time()
        reads_before = self.recorder.reads_for(self.label)
        timeout = max(60, int(self.deadline - time.time()))

        session_span = None
        if self.telemetry is not None and self.telemetry.enabled:
            # Its own trace, linked to the run: see Telemetry.span.
            session_span = self.telemetry.span(
                "session {} batch {}".format(self.label, batch_no),
                parent=self.run_span,
                new_trace=True,
                **{
                    "agent_bench.run_id": self.run_id,
                    "agent_bench.scenario": self.scenario.get("name"),
                    "agent_bench.agent": self.label,
                    "agent_bench.batch": batch_no,
                    "agent_bench.attempt": attempt + 1,
                    "agent_bench.files_assigned": len(files),
                }
            )
            telemetry_mod.set_session(self.label, session_span)
        try:
            done = subprocess.run(
                cmd, cwd=str(self.repo), env=env, capture_output=True,
                text=True, errors="replace", timeout=timeout,
            )
            log.write(done.stdout or "")
            if done.returncode != 0:
                log.write("\n[exit {}]\n{}\n".format(done.returncode,
                                                     (done.stderr or "")[-2000:]))
            status = done.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            log.write("\n[timed out after {}s]\n".format(timeout))
            status = None
            timed_out = True
        log.flush()

        missed = self._unread(files, reads_before)
        if missed:
            log.write("\n[unread: {}]\n".format(", ".join(missed)))
            log.flush()

        if session_span is not None:
            # Stop parenting request spans onto this session before ending it.
            telemetry_mod.set_session(self.label, None)
            reviewed = len(files) - len(missed)
            session_span.end(
                error="timeout" if timed_out else None,
                **{
                    "agent_bench.files_unread": len(missed),
                    "agent_bench.files_reviewed": reviewed,
                    "agent_bench.exit_code": status,
                }
            )
            tel = self.telemetry
            tel.count("agent_bench.sessions", 1, agent=self.label,
                      outcome="timeout" if timed_out else
                              ("incomplete" if missed else "complete"))
            tel.count("agent_bench.files_reviewed", reviewed, agent=self.label)
            if missed:
                tel.count("agent_bench.files_unread", len(missed),
                          agent=self.label)
            tel.log(
                "{} batch {} attempt {}: reviewed {} of {} files in {:.0f}s".format(
                    self.label, batch_no, attempt + 1, reviewed, len(files),
                    time.time() - started),
                span=session_span, agent=self.label, batch=batch_no,
                files_unread=len(missed),
            )

        return {
            "batch": batch_no,
            "attempt": attempt + 1,
            "files": files,
            "missed": missed,
            "exit": status,
            "timed_out": timed_out,
            "t_start": started,
            "t_end": time.time(),
        }


# -- server metrics ------------------------------------------------------


def resolve_opencode():
    """Find the real opencode executable, not a wrapper script.

    opencode ships as a native binary. npm also installs a POSIX `sh` shim
    beside it, and on Windows that shim mangles multi-line arguments: the
    agent receives only the first line of its prompt, replies "your message
    got cut off", and the run produces traffic with no work in it. Inside the
    container PATH resolves to the binary and this is moot, but local runs on
    Windows need the .exe directly.
    """
    override = os.environ.get("OPENCODE_BIN")
    if override:
        return override
    if os.name == "nt":
        found = shutil.which("opencode.exe")
        if found:
            return found
        try:
            root = subprocess.run(["npm", "root", "-g"], capture_output=True,
                                  text=True, timeout=60, shell=True).stdout.strip()
        except Exception:
            root = ""
        if root:
            exe = Path(root) / "opencode-ai" / "bin" / "opencode.exe"
            if exe.exists():
                return str(exe)
    return shutil.which("opencode") or "opencode"


def smoke_opencode(opencode_bin, cfg_text, model, timeout=180, attempts=2):
    """One real opencode turn, on a throwaway file, before the run starts.

    The wire checks in preflight.py cannot catch everything: a model can
    stream well-formed content and well-formed tool calls and still leave
    opencode producing nothing at all, in which case a run reports plausible
    throughput for no work. Driving the real agent is the only way to see it,
    and seeing it here costs a few minutes instead of a whole run.

    Retried, because this check fails open-endedly: a cold provider install
    or a slow minute on the gateway looks exactly like an incompatible model,
    and aborting a half-hour benchmark over one slow attempt is worse than
    waiting twice.
    """
    with tempfile.TemporaryDirectory(prefix="agent-bench-smoke-") as tmp:
        sample = Path(tmp) / "sample.py"
        sample.write_text(
            "def div(a, b):\n"
            "    return a / b\n"
            "\n"
            "def get(d, k):\n"
            "    return d[k]\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["OPENCODE_CONFIG_CONTENT"] = cfg_text
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        env["PWD"] = tmp  # as in Agent._one_session
        for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"),
                         ("XDG_STATE_HOME", "state"), ("XDG_CONFIG_HOME", "config")):
            d = Path(tmp) / sub
            d.mkdir(parents=True, exist_ok=True)
            env[var] = str(d)
        cmd = [opencode_bin, "run", "--pure", "-m", "bench/" + model,
               "Read sample.py and list its bugs with line numbers."]
        last = ""
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                done = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True,
                                      text=True, errors="replace", timeout=timeout)
            except subprocess.TimeoutExpired:
                last = "produced nothing in {}s".format(timeout)
                continue
            took = time.monotonic() - started
            text = (done.stdout or "").strip()
            if text:
                note = "" if attempt == 1 else " (on attempt {})".format(attempt)
                return True, "reviewed a sample file in {:.0f}s, {} chars out{}".format(
                    took, len(text), note)
            last = "exited {} after {:.0f}s with no output".format(
                done.returncode, took)

        return False, (
            "opencode {} on {} attempts against this model.\n"
            "  The gateway answers at the wire level, so the agent is not "
            "getting replies it can act on, and a run against this model "
            "would generate little or no load while appearing to succeed. "
            "Try another model, or raise SMOKE_SECONDS if the gateway is "
            "merely slow right now.".format(last, attempts)
        )


def scrape(url, timeout=20):
    if not url:
        return None
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception as exc:
        return "# scrape failed: {}: {}\n".format(type(exc).__name__, exc)


# -- main ----------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="agent-bench",
        description="Load a self-hosted LLM with concurrent coding agents.")
    ap.add_argument("--scenario", default=os.environ.get("SCENARIO", "agent-framework-review"))
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", ""),
                    help="gateway base URL including /v1")
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", ""))
    ap.add_argument("--api-key", default=os.environ.get("LLM_API_KEY", ""))
    ap.add_argument("--agents", type=int, default=_int_env("AGENTS", 0))
    ap.add_argument("--batch-size", type=int, default=_int_env("BATCH_SIZE", 0))
    ap.add_argument("--max-files", type=int, default=_int_env("MAX_FILES", 0),
                    help="truncate the queue, for smoke tests")
    ap.add_argument("--wall-seconds", type=int, default=_int_env("WALL_SECONDS", 0))
    ap.add_argument("--gap-seconds", type=float,
                    default=float(os.environ.get("GAP_SECONDS", "3")),
                    help="stagger between agent starts")
    ap.add_argument("--corpus-dir", default=os.environ.get("CORPUS_DIR", "/corpus"))
    ap.add_argument("--results-dir", default=os.environ.get("RESULTS_DIR", "/results"))
    ap.add_argument("--proxy-port-base", type=int, default=_int_env("PROXY_PORT_BASE", 18800))
    ap.add_argument("--vllm-metrics-url", default=os.environ.get("VLLM_METRICS_URL", ""))
    ap.add_argument("--gateway-metrics-url", default=os.environ.get("GATEWAY_METRICS_URL", ""))
    ap.add_argument("--skip-preflight", action="store_true",
                    default=_bool_env("SKIP_PREFLIGHT"))
    ap.add_argument("--smoke-seconds", type=int, default=_int_env("SMOKE_SECONDS", 180),
                    help="how long the opencode smoke run may take before the "
                         "model is judged unusable")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the work queue and exit without calling the model")
    args = ap.parse_args(argv)

    if not args.base_url:
        sys.exit("set --base-url (or LLM_BASE_URL) to the gateway URL including /v1")
    if not args.model:
        sys.exit("set --model (or LLM_MODEL)")

    scenario, scenario_path = load_scenario(args.scenario)
    if args.agents:
        scenario["agents"] = args.agents
    if args.batch_size:
        scenario["batch_size"] = args.batch_size
    budget = scenario.get("budget") or {}
    wall = args.wall_seconds or int(budget.get("wall_seconds") or 1800)
    scenario["_model"] = args.model

    repo, sha = resolve_repo(scenario, args.corpus_dir)
    files = enumerate_files(repo, scenario)
    if args.max_files:
        files = files[:args.max_files]
    if not files:
        sys.exit("the work queue is empty: check 'paths' and the size filters "
                 "in {}".format(scenario_path))

    n_agents = max(1, int(scenario.get("agents") or 1))
    slices = shard(files, n_agents)
    n_batches = sum(len(batches([f for f, _ in s], scenario.get("batch_size", 6)))
                    for s in slices)
    total_bytes = sum(size for _, size in files)

    print("agent-bench: scenario {} | repo {}{}".format(
        scenario["name"], repo, " @ " + sha[:12] if sha else ""))
    print("  queue: {} files, {:.1f} MB, {} agents, batch {} -> {} sessions".format(
        len(files), total_bytes / 1e6, n_agents,
        scenario.get("batch_size", 6), n_batches))
    print("  model: {} via {}".format(args.model, args.base_url))

    if args.dry_run:
        for i, s in enumerate(slices):
            print("  {}: {} files".format("agent-{:02d}".format(i), len(s)))
            for f, size in s[:5]:
                print("      {:>8}  {}".format(size, f))
            if len(s) > 5:
                print("      ... {} more".format(len(s) - 5))
        return 0

    opencode_bin = resolve_opencode()
    template = (ROOT / "opencode" / "config.template.json").read_text(encoding="utf-8")

    def render_config(base_url):
        return (template
                .replace("__BASE_URL__", base_url)
                .replace("__API_KEY__", args.api_key or "unused")
                .replace("__MODEL__", args.model)
                .replace("__CONTEXT__", str(int(scenario.get("context_limit") or 131072)))
                .replace("__OUTPUT__", str(int(scenario.get("output_limit") or 16384)))
                .replace("__TASK_PERMISSION__",
                         "allow" if scenario.get("subagents") else "deny"))

    checks = []
    if not args.skip_preflight:
        print("preflight:")
        ok, checks = preflight_mod.run(
            args.base_url, args.model, args.api_key,
            max_tokens=min(2048, int(scenario.get("output_limit") or 2048)))
        if ok:
            # The wire is fine; now check the agent can actually use it. This
            # also warms opencode's provider package, so the first real
            # session does not pay for the install.
            ok, detail = smoke_opencode(opencode_bin, render_config(args.base_url),
                                        args.model, timeout=args.smoke_seconds)
            print("  [{}] {:<24} {}".format("ok" if ok else "FAIL",
                                            "opencode smoke run", detail),
                  flush=True)
            checks.append(("opencode smoke run", ok, detail))
        if not ok:
            print("preflight FAILED: not generating load against a model the "
                  "agents cannot use. Override with --skip-preflight.")
            return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.results_dir) / "{}-{}".format(scenario["name"], stamp)
    (out_dir / "logs").mkdir(parents=True, exist_ok=True)

    goal_template = _strip_frontmatter(
        (ROOT / "opencode" / "command" / "goal.md").read_text(encoding="utf-8"))

    run_id = "{}-{}".format(scenario["name"], stamp)
    telemetry = telemetry_mod.from_env(run_id, scenario["name"], args.model)
    if telemetry.enabled:
        print("telemetry: OTLP to {} every {:.0f}s".format(
            telemetry.endpoint, telemetry.flush_seconds))
        telemetry.start()
    run_span = telemetry.span(
        "agent-bench run {}".format(scenario["name"]),
        **{
            "agent_bench.run_id": run_id,
            "agent_bench.scenario": scenario["name"],
            "agent_bench.agents": n_agents,
            "agent_bench.queue_files": len(files),
            "agent_bench.batch_size": scenario.get("batch_size"),
            "agent_bench.repo_sha": sha,
            "gen_ai.request.model": args.model,
            "server.address": args.base_url,
        }
    )

    recorder = proxy_mod.Recorder(str(out_dir / "requests.jsonl"))
    servers = []
    cfg_paths = []
    for i in range(n_agents):
        port = args.proxy_port_base + i
        servers.append(proxy_mod.serve(
            port, args.base_url.rstrip("/").removesuffix("/v1"),
            args.api_key, recorder, "agent-{:02d}".format(i),
            telemetry if telemetry.enabled else None))
        cfg = render_config("http://127.0.0.1:{}/v1".format(port))
        p = out_dir / "config" / "agent-{:02d}.json".format(i)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(cfg, encoding="utf-8")
        cfg_paths.append(p)

    before = {
        "vllm": scrape(args.vllm_metrics_url),
        "gateway": scrape(args.gateway_metrics_url),
    }
    if before["vllm"]:
        (out_dir / "vllm-before.prom").write_text(before["vllm"], encoding="utf-8")
    if before["gateway"]:
        (out_dir / "gateway-before.prom").write_text(before["gateway"], encoding="utf-8")

    # opencode's own state (a sqlite db and a copied ripgrep per agent) is
    # bulky and uninteresting afterwards, so it lives outside the bundle.
    state_root = Path(os.environ.get("AGENT_BENCH_STATE")
                      or (Path(tempfile.gettempdir()) / ("agent-bench-" + stamp)))
    state_root.mkdir(parents=True, exist_ok=True)

    stop_event = threading.Event()
    t0 = time.time()
    deadline = t0 + wall
    agents = []
    for i, sl in enumerate(slices):
        a = Agent(i, sl, scenario, cfg_paths[i], repo, out_dir, goal_template,
                  deadline, stop_event, opencode_bin, recorder, state_root,
                  telemetry, run_span, run_id)
        agents.append(a)

    # Sample live gauges while the run is in flight, so the dashboards show it
    # happening rather than only a verdict at the end.
    sampler_stop = threading.Event()

    def sample_gauges():
        while not sampler_stop.wait(5.0):
            telemetry.gauge("agent_bench.requests.in_flight", proxy_mod.inflight())
            telemetry.gauge("agent_bench.agents.running",
                            sum(1 for a in agents if a.is_alive()))

    sampler = None
    if telemetry.enabled:
        sampler = threading.Thread(target=sample_gauges, daemon=True,
                                   name="agent-bench-gauges")
        sampler.start()

    print("running: {} agents, {}s budget, results in {}".format(
        n_agents, wall, out_dir))
    for a in agents:
        a.start()
        print("  started {} ({} files, {} batches)".format(
            a.label, len(a.assigned), len(a.batches)), flush=True)
        if args.gap_seconds:
            time.sleep(args.gap_seconds)

    try:
        for a in agents:
            a.join(timeout=max(1, deadline - time.time()) + 120)
    except KeyboardInterrupt:
        print("\ninterrupted: stopping agents")
        stop_event.set()
        for a in agents:
            a.join(timeout=30)

    elapsed = time.time() - t0
    sampler_stop.set()
    if sampler:
        sampler.join(timeout=10)
    for s in servers:
        s.shutdown()
    recorder.close()

    after = {
        "vllm": scrape(args.vllm_metrics_url),
        "gateway": scrape(args.gateway_metrics_url),
    }
    if after["vllm"]:
        (out_dir / "vllm-after.prom").write_text(after["vllm"], encoding="utf-8")
    if after["gateway"]:
        (out_dir / "gateway-after.prom").write_text(after["gateway"], encoding="utf-8")

    meta = {
        "scenario": {k: v for k, v in scenario.items() if not k.startswith("_")},
        "scenario_file": str(scenario_path),
        "repo": str(repo),
        "repo_sha": sha,
        "model": args.model,
        "base_url": args.base_url,
        "agents": n_agents,
        "queue_files": len(files),
        "queue_bytes": total_bytes,
        "planned_sessions": n_batches,
        "wall_seconds_budget": wall,
        "elapsed_seconds": round(elapsed, 1),
        "opencode_version": os.environ.get("OPENCODE_VERSION"),
        "opencode_bin": opencode_bin,
        "started_utc": datetime.fromtimestamp(t0, timezone.utc).isoformat(),
        "preflight": [{"check": c, "ok": o, "detail": d} for c, o, d in checks],
        "agent_sessions": [
            {"agent": a.label, "assigned": len(a.assigned), "sessions": a.sessions}
            for a in agents
        ],
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    if not _bool_env("KEEP_AGENT_STATE"):
        shutil.rmtree(state_root, ignore_errors=True)

    summary = report_mod.build(out_dir, meta, files, scenario)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    text = report_mod.render(summary)
    (out_dir / "summary.txt").write_text(text, encoding="utf-8")
    print("\n" + text)
    print("results: {}".format(out_dir))

    _export_summary(telemetry, run_span, summary, run_id)
    telemetry.shutdown()
    if telemetry.enabled:
        print("telemetry: exported {spans} spans, {metrics} metrics, {logs} logs"
              "{failed}".format(failed=", {} export failures".format(telemetry.failures)
                                if telemetry.failures else "",
                                **telemetry.exported))
    return 0


def _export_summary(telemetry, run_span, summary, run_id):
    """The run's verdict as gauges plus one log record.

    The per-request counters already describe the traffic. These are the
    derived figures a dashboard wants to show per run, and which cannot be
    recomputed from counters alone -- coverage, the per-stream rate at the
    slow end, cost per reviewed file.
    """
    if not telemetry.enabled:
        return
    load = summary.get("load") or {}
    prod = summary.get("productivity") or {}
    gauges = {
        "agent_bench.run.output_tokens_per_second": load.get("output_tokens_per_second"),
        "agent_bench.run.prompt_tokens_per_second": load.get("prompt_tokens_per_second"),
        "agent_bench.run.cached_prompt_fraction": load.get("cached_prompt_fraction"),
        "agent_bench.run.mean_concurrent_requests": load.get("mean_concurrent_requests"),
        "agent_bench.run.ttft_p50_seconds": load.get("ttft_p50_s"),
        "agent_bench.run.ttft_p95_seconds": load.get("ttft_p95_s"),
        "agent_bench.run.per_stream_tps_p50": load.get("per_stream_tps_p50"),
        "agent_bench.run.per_stream_tps_slow_decile": load.get("per_stream_tps_p90_slow"),
        "agent_bench.run.prompt_tokens_p50": load.get("prompt_tokens_p50"),
        "agent_bench.run.coverage": prod.get("coverage"),
        "agent_bench.run.files_reviewed": prod.get("files_reviewed"),
        "agent_bench.run.files_abandoned": prod.get("files_abandoned"),
        "agent_bench.run.output_tokens_per_reviewed_file":
            prod.get("output_tokens_per_reviewed_file"),
        "agent_bench.run.prompt_tokens_per_reviewed_file":
            prod.get("prompt_tokens_per_reviewed_file"),
        "agent_bench.run.elapsed_seconds": summary.get("elapsed_seconds"),
    }
    for name, value in gauges.items():
        if value is not None:
            telemetry.gauge(name, float(value))

    meets = load.get("meets_interactivity_floor")
    telemetry.log(
        "run {} finished: {} tok/s output, {} of {} files reviewed "
        "({}), {} agents".format(
            run_id, load.get("output_tokens_per_second"),
            prod.get("files_reviewed"), prod.get("queue_files"),
            "meets the interactivity floor" if meets
            else "below the interactivity floor" if meets is False
            else "no floor set",
            summary.get("agents")),
        span=run_span,
        **{
            "agent_bench.run_id": run_id,
            "agent_bench.coverage": prod.get("coverage"),
            "agent_bench.meets_interactivity_floor": meets,
            "agent_bench.requests_failed": load.get("requests_failed"),
        }
    )
    run_span.end(**{
        "agent_bench.output_tokens_per_second": load.get("output_tokens_per_second"),
        "agent_bench.coverage": prod.get("coverage"),
        "agent_bench.files_reviewed": prod.get("files_reviewed"),
        "agent_bench.requests_ok": load.get("requests_ok"),
        "agent_bench.requests_failed": load.get("requests_failed"),
        "agent_bench.meets_interactivity_floor": meets,
    })


def _strip_frontmatter(text):
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    return parts[2].lstrip("\n") if len(parts) == 3 else text


def _int_env(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _bool_env(name):
    return (os.environ.get(name, "") or "").lower() in ("1", "true", "yes")


if __name__ == "__main__":
    sys.exit(main())
