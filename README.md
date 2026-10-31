# wuyi-labs
My personal tech playground for experiments, code samples, demos, and resources across AI, cloud computing, and software engineering.

## Contents

- [2610-h100-qwen-38-27b](2610-h100-qwen-38-27b/): serve Qwen3.8-27B from one H100 Azure Spot VM with vLLM, LiteLLM and an OpenTelemetry-based Grafana dashboard that tracks token economics. Blog post: [Qwen3.8-27B on a $2 H100 Azure Spot VM: does it pay off?](https://techcommunity.microsoft.com/blog/appsonazureblog/qwen3-8-27b-on-a-2-h100-azure-spot-vm-does-it-pay-off/4563387).
- [2610-h100-qwen-38-flash-next](2610-h100-qwen-38-flash-next/): serve Qwen3.8-Flash-Next across the two H100s of one Azure Spot VM with vLLM, observe it on a multi-model Grafana dashboard, and load it with read-only coding agents. Blog post: [Qwen3.8-Flash-Next on two H100s in an Azure Spot VM](https://techcommunity.microsoft.com/blog/appsonazureblog/qwen3-8-flash-next-on-two-h100s-in-an-azure-spot-vm/4563893).
- [2610-rtx-qwen-38-27b](2610-rtx-qwen-38-27b/): serve Qwen3.8-27B on an RTX Spark laptop with vLLM and LiteLLM, and watch it with the OpenTelemetry Collector in Azure Monitor, Application Insights and Azure Managed Grafana: GPU and unified-memory metrics from Windows, LiteLLM traces, a dashboard and Prometheus alert rules. Blog post: [Qwen3.8-27B on a Lenovo laptop: run it on RTX Spark, watch it in Azure Monitor](https://techcommunity.microsoft.com/blog/appsonazureblog/qwen3-8-27b-on-a-lenovo-laptop-run-it-on-rtx-spark-watch-it-in-azure-monitor/4567828).
- [agent-bench](agent-bench/): a container that loads a self-hosted LLM with concurrent read-only coding agents over pinned .NET, TypeScript and Microsoft Agent Framework code, and reports both the throughput it drew and how much review work the agents actually finished.
