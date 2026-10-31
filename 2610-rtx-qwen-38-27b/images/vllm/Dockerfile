# syntax=docker/dockerfile:1.7

# The official vLLM image publishes a native linux/arm64 CUDA 13 variant.
# 0.32 serves this model through vLLM's Model Runner V2 and decodes 7-9% more
# steps a second than 0.31 on the same laptop and prompts, with the same
# evaluation results (see the README). 0.28+ adds DFlash 2 block drafting,
# and xgrammar 0.2.7+ exposes normalize_tool_choice for OpenAI-style tool calls.
FROM vllm/vllm-openai:v0.32.0@sha256:befccf0e7e4089aa23926bb6f887a85c0d46c2a99e6de5c5ffdf5cb7957c5b34

ARG MODEL_ID=unsloth/Qwen3.8-27B-NVFP4
ARG MODEL_REVISION=16b6615af3548b88e2d8e382457bc705b00479cf
ARG MODEL_DIR=/models/Qwen3.8-27B-NVFP4
ARG DRAFT_ID=incoai/Qwen3.8-27B-DFlash2
ARG DRAFT_REVISION=015e795645c74b1a0eeef3b570031fb62e769bc5
ARG DRAFT_DIR=/models/Qwen3.8-27B-DFlash2

RUN hf download "${MODEL_ID}" \
      --revision "${MODEL_REVISION}" \
      --local-dir "${MODEL_DIR}"

# DFlash 2 drafts a seven-token block in one pass of a 1.86B-parameter
# drafter instead of three sequential MTP passes, which suits this
# memory-bandwidth-bound GPU. The serving command quantizes it to FP8 online.
RUN hf download "${DRAFT_ID}" \
      --revision "${DRAFT_REVISION}" \
      --local-dir "${DRAFT_DIR}"

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    VLLM_ENABLE_V1_MULTIPROCESSING=0 \
    VLLM_WORKER_MULTIPROC_METHOD=spawn

LABEL org.opencontainers.image.title="Qwen3.8-27B NVFP4 vLLM server" \
      org.opencontainers.image.source="https://huggingface.co/unsloth/Qwen3.8-27B-NVFP4" \
      ai.runtime.name="vLLM" \
      ai.runtime.version="0.32.0" \
      ai.model.base="Qwen/Qwen3.8-27B" \
      ai.model.repository="unsloth/Qwen3.8-27B-NVFP4" \
      ai.model.revision="16b6615af3548b88e2d8e382457bc705b00479cf" \
      ai.model.quantization="NVFP4 (compressed-tensors, Unsloth Dynamic V3.0 preview)" \
      ai.model.draft.repository="incoai/Qwen3.8-27B-DFlash2" \
      ai.model.draft.revision="015e795645c74b1a0eeef3b570031fb62e769bc5" \
      ai.model.license="Apache-2.0"

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15m --retries=5 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=10)"]

# The base image's entrypoint is `vllm serve`; clear it so this command and
# the Compose command run as written. Profile up to four 1024-by-1024 images
# and shrink the duplicated multimodal processor cache for this UMA host.
# Block size 64 makes vLLM round the hybrid attention block up to 1,664
# tokens, a multiple of the drafter's 64-token sliding-window kernel block.
# With the default 1,648 the drafter pages are padded to full hybrid pages and
# a 196,608-token request needs 13.5 GiB of KV cache instead of 9.2 GiB.
# Explicit async scheduling decodes about 5% more steps a second with the
# same outputs.
ENTRYPOINT []
CMD ["python3", "-m", "vllm.entrypoints.openai.api_server", \
     "--model", "/models/Qwen3.8-27B-NVFP4", \
     "--served-model-name", "qwen3.8-27b", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--quantization", "compressed-tensors", \
     "--trust-remote-code", \
     "--limit-mm-per-prompt", "{\"image\":{\"count\":4,\"width\":1024,\"height\":1024},\"video\":0}", \
     "--mm-processor-cache-gb", "0.25", \
     "--enable-prefix-caching", \
     "--block-size", "64", \
     "--max-model-len", "262144", \
     "--max-num-seqs", "1", \
     "--max-num-batched-tokens", "8192", \
     "--async-scheduling", \
     "--gpu-memory-utilization", "0.05", \
     "--kv-cache-memory-bytes", "12616466432", \
     "--kv-cache-dtype", "fp8", \
     "--speculative-config", "{\"method\":\"dflash\",\"model\":\"/models/Qwen3.8-27B-DFlash2\",\"num_speculative_tokens\":7,\"quantization\":\"fp8\"}", \
     "--reasoning-parser", "qwen3", \
     "--enable-auto-tool-choice", \
     "--tool-call-parser", "qwen3_coder"]
