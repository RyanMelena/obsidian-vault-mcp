# obsidian-vault-mcp

[![ci](https://github.com/RyanMelena/obsidian-vault-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/RyanMelena/obsidian-vault-mcp/actions/workflows/ci.yml)
[![docker](https://github.com/RyanMelena/obsidian-vault-mcp/actions/workflows/docker.yml/badge.svg)](https://github.com/RyanMelena/obsidian-vault-mcp/actions/workflows/docker.yml)

One MCP server for an Obsidian vault: **search, read, and write**, plus a Gitea
webhook receiver that keeps the server-side working tree current.

Search and read are delegated to
[obsidian-hybrid-search](https://github.com/flowing-abyss/obsidian-hybrid-search),
which this process supervises as a child on loopback. OHS's own MCP server is
read-only — `search`, `read`, `reindex`, `status` — so agents can find notes but
never record one. Wrapping rather than forking it keeps
`npm install -g obsidian-hybrid-search@latest` a one-line upgrade.

```
desktops (Obsidian + Obsidian Git plugin)
      │  push / pull
      ▼
   Gitea ──webhook──► obsidian-vault ──git pull --rebase──► /vault
                            │                                  ▲
                            ├── save_note / update_note ───────┘
                            │
                            └── search_notes / read_notes
                                     │
                                     ▼   (loopback, child process)
                            obsidian-hybrid-search serve
                                     │
                            one MCP endpoint ──► Agent
```

The vault is a git working tree that both sides write to: desktops push through
Gitea, agents write through `save_note`. Every git operation runs behind a
single lock, and pushes this process makes are recorded so the webhook they
trigger is dropped instead of looping.

## Tools

| Tool | Purpose |
| --- | --- |
| `search_notes` | Hybrid / semantic / fulltext / title search, similar-note lookup, link-graph traversal. Scope, tag, and rerank options. |
| `read_notes` | Fetch notes in full by vault-relative path, with links and backlinks. |
| `save_note` | Write a new note into `ai-inbox/`. Server assigns id, filename, timestamp. Commits and pushes. |
| `update_note` | Revise a note in the inbox: replace the body, merge frontmatter keys, or drop them with `null`. |
| `sync_vault` | Pull from the remote immediately. Rarely needed — the webhook handles this. |
| `vault_status` | Vault path, branch, HEAD, inbox note count. |

## Swapping the search backend

`SearchBackend` in `backends.py` is the seam: a five-method protocol
(`start`/`stop`/`search`/`read`/`reindex`). `OHSBackend` implements it,
`NullBackend` disables it, and the MCP tools never reference OHS directly.

This is deliberately an adapter rather than a generic tool proxy. A proxy that
re-exported OHS's tool list verbatim would be shorter, but it would make OHS's
schema the public API.  With an adapter, replacing OHS means one new class and one env var;
agents that learned `search_notes` keep working.

The backend talks JSON-RPC to a long-lived `serve` process rather than shelling
out per query. Measured on a trivial vault: ~515 ms per cold CLI invocation
versus ~10 ms per call against the running server. Node startup, database open,
and the embedding-endpoint probe are paid once, not per search.

Every note gets `id` (ULID), `title`, `created`, and `status: active`. Once
they're there, superseding a note is `update_note(path, frontmatter={"status": "superseded", "superseded_by": "<id>"})`
and every agent query becomes `--frontmatter -status:superseded`.


`pytest tests/` covers all of the above.

## Reranking is CPU-only

`--rerank` cannot use a GPU. `reranker.js` hardcodes `device: 'cpu'` with
`dtype: 'int8'`, and the embedder does the same with `dtype: 'q8'`. There is no
env override. The upstream comment explains why: CUDA and CoreML don't support
the quantized ONNX opsets, so `device: 'auto'` silently falls back to fp32 and
blows up to roughly 36 GB of memory.

## Embedding model note

Ollama defaults `num_ctx` to 2048. OHS chunks by heading, so a long section is
silently truncated at embed time with no error and no way to pass the parameter
through. Build a tagged model instead:

```
FROM bge-m3
PARAMETER num_ctx 8192
```

```sh
ollama create bge-m3-8k -f Modelfile
```

Then set `OPENAI_EMBEDDING_MODEL=bge-m3-8k`.

BGE-M3 over the higher-scoring Qwen3-Embedding models for a specific reason:
Qwen3 is instruction-aware and needs `Instruct: <task>\nQuery: <query>` prefixing
to reach its benchmark numbers, and OHS makes a plain `/v1/embeddings` call with
no prefixing. BGE-M3 is symmetric and prefix-free, so it performs as advertised
through a generic client. Recover precision with OHS's `--rerank` instead.

Changing the embedding model requires `reindex --force` on every index.

## Running as an arbitrary UID

The image defaults to `3000:3000` but works under any `user: "<uid>:<gid>"`,
so you can match whatever owns the vault dataset without rebuilding.

Two things break in a container whose UID has no `/etc/passwd` entry, and the
entrypoint handles both:

- **`getpwuid()` fails.** The OpenSSH client exits with "You don't exist, go
  away!" before it ever contacts the remote, so git-over-SSH dies. The
  entrypoint detects an unknown UID and uses `nss_wrapper` to synthesize
  passwd and group entries via `LD_PRELOAD` — no writable `/etc/passwd`
  required, which is what the usual `chmod g+w /etc/passwd` trick needs and
  which only works when the GID happens to be 0.
- **Git refuses the repo.** A working tree owned by another UID trips
  "detected dubious ownership". The entrypoint writes `safe.directory` into a
  gitconfig under `/state` and points `GIT_CONFIG_GLOBAL` at it, rather than
  `$HOME`, which may not be writable.

Everything the process writes lives under `/state` or `$HOME`, both `0777` in
the image. Use the named volume for `/state`; it inherits that mode on first
use. If you bind-mount a host path there instead, chown it to the UID you run
as or the container exits at startup with a clear message rather than failing
later in a confusing way.

One thing the image cannot fix: **the SSH deploy key must be readable by the
UID you run as**, and OpenSSH still refuses group- or world-readable private
keys. Chown the mounted key to that UID with mode `0600`. If that is awkward,
use an HTTPS remote with a token instead — nothing else here depends on SSH.

### Release

```sh
git tag v0.2.0 && git push --tags
```

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e . pytest pytest-asyncio ruff
PYTHONPATH=src .venv/bin/python -m pytest tests -q
```

Run without git for local experimentation:

```sh
VAULT_PATH=/tmp/vault GIT_ENABLED=false .venv/bin/python -m obsidian_vault_mcp
```

## License

MIT
