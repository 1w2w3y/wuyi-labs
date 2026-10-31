# Recorded runs

Headline numbers from agent-bench runs worth keeping. Full bundles stay in
`results/`, which Git ignores.

## Qwen3.8-27B, one agent, six files

Qwen3.8-27B served by vLLM behind a LiteLLM gateway, under the alias
`local-llm`, as in this repository's Qwen3.8-27B samples. The model ran on one
machine, and the agent ran in Docker on a second machine on the same local
network. One agent reviewed the first six files of the `agent-framework-review`
queue (`AGENTS=1`, `MAX_FILES=6`, 69,534 bytes) at corpus commit
`ebcf4db7f946`, with opencode 1.19.0. The gateway returned no cached-token
counts, so these runs have no prompt-cache figure. Six files is a small sample:
use it to check the setup, not to plan capacity.

| | `20261031T010103Z` (invalid) | `20261031T011604Z` | `20261031T035526Z` |
|---|---|---|---|
| Harness | image default, before the fixes | same image, `PWD` unset by hand | image default, with both fixes |
| Files actually read | **0 of 6** | 6 of 6 | 6 of 6 |
| Coverage as reported | 6 of 6 (wrong) | 6 of 6 | 6 of 6 |
| Elapsed | 714.6 s | 1,173.7 s | 885.0 s |
| Requests | 17 ok, 0 failed | 12 ok, 0 failed | 8 ok, 0 failed |
| Output | 27.4 tok/s, 19,611 tokens | 24.6 tok/s, 28,838 tokens | 23.6 tok/s, 20,928 tokens |
| Reasoning, of the output | 17,325 tokens | 26,368 tokens | 19,093 tokens |
| Prompt tokens, total | 300,177 | 483,364 | 163,757 |
| Prompt size, p50 / p95 | 8,083 / 37,496 | 53,411 / 56,800 | 13,302 / 43,200 |
| TTFT, p50 / p95 | 2.03 s / 9.87 s | 3.21 s / 15.83 s | 3.18 s / 12.81 s |
| Per-stream rate, p50 / slowest decile | 32.5 / 28.5 tok/s | 34.0 / 25.9 tok/s | 27.4 / 25.0 tok/s |
| Cost per file | not meaningful | 4,806 output, 80,561 prompt tokens | 3,488 output, 27,293 prompt tokens |

The first run shows why a clean exit and a good coverage line aren't enough.
opencode started in `/opt/agent-bench` rather than the corpus, so every read of
an assigned file was denied, and the agent spent the run reading the harness.
The harness counted the attempts as reviews. Ignore its numbers as a measure of
review work. Both bugs are fixed; see the Git history of `runner/`.

The two valid runs reviewed the same files with the same settings, yet the
third took 25% less time and 27% fewer output tokens, because the model did less
checking beyond the six files. That spread is the model's own, and a reason to
treat a run this small as a rough figure. In both, about 91% of the output was
reasoning, and two requests took most of the time: 1,001 of 1,174 seconds, then
739 of 885. At 2.5 to 3.3 minutes per file, 9 to 12 files fit the default
30-minute budget with one agent.
