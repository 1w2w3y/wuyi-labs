# Working in this repository

This repository is public, so anyone can read everything checked in.

## Keep personal details out

Don't put personal or private details in files, commit messages or pull
requests: people's names and email addresses, local usernames, home directories
and other local paths, machine names, private network addresses, gateway URLs,
keys or tokens. Use placeholders in examples, such as
`https://gateway.example.com/v1`.

Check the diff for these before committing. Benchmark output is the usual way
in: an agent-bench `meta.json` records the gateway URL, and logs and metric
scrapes can carry hostnames, so bundles under `agent-bench/results/` stay out
of Git.

## Benchmark numbers

Numbers are welcome, with a basic description of how the model was hosted: the
model, the serving stack, the class of hardware, and any limit that shapes the
result. Describe the setup without identifying the machine or where it is.
Record them in [`agent-bench/RESULTS.md`](agent-bench/RESULTS.md).

`agent-bench/opencode/AGENTS.md` is a different file: it instructs the review
workers inside the benchmark container, not agents working on this repository.
