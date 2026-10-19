# syntax=docker/dockerfile:1
#
# agent-bench: load a self-hosted LLM with real concurrent coding agents.
#
# Build:  docker build -t agent-bench .
#         docker build --build-arg BAKE_CORPUS=1 -t agent-bench .   # corpus in the image
#
# The default image carries no corpus: fetch.sh clones it on first run into
# /corpus, which you should mount as a named volume so later runs reuse it.
ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE}

# Pin opencode: the agent under test is part of the measurement.
ARG OPENCODE_VERSION=1.19.0
# 1 clones the corpus at build time, for air-gapped or strictly pinned runs.
ARG BAKE_CORPUS=0
# Corporate networks often block registry.npmjs.org and mirror it instead.
# Point this at your feed to build behind one, e.g.
#   --build-arg NPM_REGISTRY=https://your-feed.example.com/npm/
ARG NPM_REGISTRY=https://registry.npmjs.org/

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl python3 python3-yaml procps \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --registry="${NPM_REGISTRY}" "opencode-ai@${OPENCODE_VERSION}" \
    && npm cache clean --force \
    && opencode --version

ENV AGENT_BENCH_HOME=/opt/agent-bench \
    CORPUS_DIR=/corpus \
    RESULTS_DIR=/results \
    HOME=/root \
    PYTHONUNBUFFERED=1 \
    OPENCODE_VERSION=${OPENCODE_VERSION}

WORKDIR /opt/agent-bench
# goal.md is kept once on disk. opencode has used both spellings for the
# custom-command directory and the runner renders the file itself regardless,
# so the build ships both and neither copy can drift from the other.
COPY corpus/ corpus/
COPY opencode/ opencode/
COPY scenarios/ scenarios/
COPY runner/ runner/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh corpus/fetch.sh \
    && mkdir -p "${CORPUS_DIR}" "${RESULTS_DIR}" \
    && mkdir -p opencode/commands \
    && cp opencode/command/goal.md opencode/commands/goal.md

RUN if [ "${BAKE_CORPUS}" = "1" ]; then corpus/fetch.sh; fi

# opencode resolves its provider npm package on first use; preflight warms it.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["run"]
