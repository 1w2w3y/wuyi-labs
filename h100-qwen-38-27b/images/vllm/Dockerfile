# syntax=docker/dockerfile:1.7

# NGC vLLM 26.07. The digest is the multi-arch index, so it resolves to
# linux/amd64 on the Azure VM.
FROM nvcr.io/nvidia/vllm:26.07-py3@sha256:95c498a475142c20c989c65e5d223348c09fed83ba17ddf44f117610c0bd3268

# NVIDIA 26.07 ships xgrammar 0.2.0, but its vLLM frontend imports
# normalize_tool_choice (added in xgrammar 0.2.1). Without this, every
# OpenAI tool-calling request fails with HTTP 500. 0.2.4 has no cp312
# x86_64 wheel; 0.2.3 is the closest x86_64 release.
# The digest comes from PyPI's release metadata.
ADD --checksum=sha256:fdf081fab29694302d41d61dcf52fad7d253879a718bc6afc68db0a0dabd7f19 \
    https://files.pythonhosted.org/packages/d4/fd/5ebd5d14b8993cb225151bbb8f2011742fc7a7d94a3bdbc3ec3954b9b62d/xgrammar-0.2.3-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl \
    /tmp/xgrammar-0.2.3-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl

RUN python3 -m pip install --no-cache-dir --no-deps \
      /tmp/xgrammar-0.2.3-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl \
    && rm -f /tmp/xgrammar-*.whl \
    && python3 -c "import xgrammar; assert hasattr(xgrammar, 'normalize_tool_choice')"

LABEL org.opencontainers.image.title="NGC vLLM 26.07 with xgrammar 0.2.3" \
      ai.runtime.name="NVIDIA vLLM" \
      ai.runtime.version="26.07"
