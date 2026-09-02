# obsidian-vault-mcp

[![ci](https://github.com/ryanmelena/mcp-obsidian-vault/actions/workflows/ci.yml/badge.svg)](https://github.com/ryanmelena/mcp-obsidian-vault/actions/workflows/ci.yml)
[![docker](https://github.com/ryanmelena/mcp-obsidian-vault/actions/workflows/docker.yml/badge.svg)](https://github.com/ryanmelena/mcp-obsidian-vault/actions/workflows/docker.yml)

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

## Error handling

Expected failures — a bad path, a missing note, an unreachable search backend,
a vault the container cannot write to — are raised as `ToolError`, so the
message reaches the agent verbatim instead of the SDK's generic
"Error executing tool <name>". Anything unexpected still logs a full traceback
in the container; `docker logs` is the place to look when a message is generic.

When provisioning cannot produce a usable repo, git is disabled with the reason
recorded rather than left to explode on the first write. `vault_status` reports
`git_problem` and every `save_note` returns it alongside the saved path. Search
and write keep working.

Git failures are reported, not raised. By the time a commit runs, the note is
already on disk, so raising would make an agent retry and write a duplicate on
every attempt. `save_note` returns the path plus
`"git": {"status": "error", "error": "..."}` and the next successful sync picks
the note up.

`pytest tests/` covers all of the above.

## Setup

### 1. Vault dataset

```sh
zfs create perfpool/obsidian
zfs set atime=off perfpool/obsidian
mkdir /mnt/perfpool/obsidian/vault
```

That's all. Create an empty repo in Gitea, set `GIT_REMOTE_URL`, and the
server provisions the rest at startup — `git init`, the `.gitignore`, the
remote, the initial commit, and the first push. Set `GIT_AUTO_INIT=false` to
manage the repo yourself.

What it does depends on what it finds, and only unambiguous cases are
automatic:

| Vault | Remote | Action |
| --- | --- | --- |
| empty or unversioned | empty | `git init`, write `.gitignore`, commit, push |
| empty | has history | clone |
| has files, no `.git` | has history | **stop** and report |
| already a repo | any | fix the remote URL, check out the branch |

The third row is a merge decision, not a provisioning step — reaching for
`--allow-unrelated-histories` on someone's knowledge base is not a choice a
daemon should make at startup. It leaves your files untouched and tells you to
clone elsewhere and merge by hand, or empty the vault so it can clone.

The generated `.gitignore` matters more than it looks. OHS writes its SQLite
index to `.obsidian-hybrid-search.db` **inside the vault root**; committing it
means three machines writing a multi-hundred-megabyte binary to the same path.

### 2. Remote authentication

**HTTP(S) with a token (recommended; no SSH needed).** In Gitea: **Settings →
Applications → Generate New Token**, scope `write:repository`. Then set
`GIT_REMOTE_URL` and either `GIT_TOKEN` or `GIT_TOKEN_FILE`.

Gitea accepts an access token as the HTTP password with any username. The
entrypoint writes it to a `0600` credential file under `/state` and configures
`credential.helper=store`, so the token stays out of `.git/config`, out of
`git remote -v`, and out of the process list — unlike the common
`https://user:token@host/repo.git` shortcut, which leaks it into all three.
`credential.useHttpPath=false` means one entry covers every repo on that host.

`GIT_TERMINAL_PROMPT=0` is set, so a bad or missing token fails immediately
instead of hanging on a password prompt.

If Gitea sits behind an internal CA rather than a public cert, mount the CA
bundle and set `GIT_SSL_CAINFO`. Don't reach for `GIT_SSL_NO_VERIFY`.

**SSH (alternative).** Generate a keypair, add the public half as a write
deploy key, and mount the private half at `/home/app/.ssh` with a `known_hosts`
entry. The key must be owned by the UID you run as, mode `0600`.

### 3. Build note

`onnxruntime-node`'s postinstall downloads CUDA binaries from `api.nuget.org`.
If your build host has restricted egress, the image build fails there. The
Dockerfile passes `--onnxruntime-node-install=skip`; nothing is lost, because
OHS never uses the CUDA execution provider (see below).

Also worth knowing: OHS degrades rather than dies when the embedding endpoint is
unreachable — it logs a warning, disables semantic search and indexing, and
keeps serving fulltext and title search. An Ollama outage costs you recall, not
availability.

### 4. Run

```sh
cp .env.example .env
openssl rand -hex 32   # -> GITEA_WEBHOOK_SECRET
docker compose -f docker-compose.example.yml up -d --build
curl -s http://obsidian-vault:3940/healthz
```

### 5. Gitea webhook

Repo → Settings → Webhooks → Gitea:

- **Target URL**: `https://obsidian-vault.internal.example.com/hooks/gitea`
- **HTTP Method**: POST, **Content Type**: `application/json`
- **Secret**: the value of `GITEA_WEBHOOK_SECRET`
- **Trigger**: Push events only

Gitea's delivery log is your debugging surface — it shows the request, response
body, and status. A 202 means the pull was queued; 401 means the secret doesn't
match; 200 with `skipped` means the push was to another branch or was this
service's own push coming back around.

`scripts/gitea-post-receive.sh` is a fallback if the Gitea container can't reach
the service over HTTP. Prefer the webhook.

### 6. Open WebUI

Register one server:

- `https://obsidian-vault.internal.example.com/mcp`

It speaks MCP Streamable HTTP natively, so `mcpo` is pure overhead here.

### 7. Desktops

Obsidian Git plugin, pointed at the same Gitea repo. Set auto-commit and
auto-pull to 2–5 minutes. Nothing else to install per client.

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
