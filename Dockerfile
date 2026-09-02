# One image, one process tree: the Python MCP server supervises
# `obsidian-hybrid-search serve` as a child on loopback.
#
# Runs as uid 3000 by default, but works under any `user: <uid>:<gid>` in
# compose — see docker-entrypoint.sh and "Running as an arbitrary UID" in
# the README.
FROM node:22-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git openssh-client ca-certificates tini python3 python3-venv libnss-wrapper \
 && rm -rf /var/lib/apt/lists/*

# onnxruntime-node's postinstall pulls CUDA binaries from api.nuget.org, which
# fails on build hosts with restricted egress. Skipping them costs nothing:
# OHS hardcodes device:'cpu' for both the embedder and the reranker, so the
# CUDA execution provider is never used. See "Reranking is CPU-only" in README.
RUN npm install -g obsidian-hybrid-search --onnxruntime-node-install=skip

ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN python3 -m venv "$VIRTUAL_ENV"

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

# Everything the process writes lives under /state or $HOME. Both are 0777 so
# the image works with any uid:gid the operator picks; a named volume mounted
# at /state inherits these permissions on first use.
RUN mkdir -p /state /home/app \
 && chmod 0777 /state /home/app

ENV HOME=/home/app \
    STATE_DIR=/state \
    XDG_CACHE_HOME=/state/cache \
    HF_HOME=/state/huggingface \
    GIT_CONFIG_GLOBAL=/state/gitconfig \
    VAULT_PATH=/vault \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=3940 \
    OHS_SPAWN=true \
    OHS_PORT=3939

USER 3000:3000
EXPOSE 3940
VOLUME ["/state"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["obsidian-vault-mcp"]
