# wuyi-labs
My personal tech playground for experiments, code samples, demos, and resources across AI, cloud computing, and software engineering.

## Contents

- [2610-h100-qwen-38-27b](2610-h100-qwen-38-27b/): serve Qwen3.8-27B from one H100 Azure Spot VM with vLLM, LiteLLM and an OpenTelemetry-based Grafana dashboard that tracks token economics.
- [2610-h100-qwen-38-flash-next](2610-h100-qwen-38-flash-next/): serve Qwen3.8-Flash-Next across the two H100s of one Azure Spot VM with vLLM, observe it on a multi-model Grafana dashboard, and load it with read-only coding agents.
- [agent-bench](agent-bench/): a container that loads a self-hosted LLM with concurrent read-only coding agents over pinned .NET, TypeScript and Microsoft Agent Framework code, and reports both the throughput it drew and how much review work the agents actually finished.
