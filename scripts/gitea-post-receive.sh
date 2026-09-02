#!/bin/sh
# Fallback for the webhook: a Gitea server-side post-receive hook.
#
# Prefer the webhook (Settings -> Webhooks -> Gitea) — it is HMAC-signed, has
# retries, and gives you a delivery log. Use this only if you cannot reach the
# vault service over HTTP from the Gitea container.
#
# Install at: <gitea-repo-root>/vault.git/hooks/post-receive.d/vault-mcp
set -eu

ENDPOINT="${VAULT_MCP_ENDPOINT:-http://obsidian-vault:3940/hooks/gitea}"
SECRET="${VAULT_MCP_SECRET:?VAULT_MCP_SECRET must be set}"
BRANCH="${VAULT_MCP_BRANCH:-main}"

while read -r _old new ref; do
    [ "$ref" = "refs/heads/$BRANCH" ] || continue
    body="{\"ref\":\"$ref\",\"after\":\"$new\"}"
    sig=$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print $NF}')
    curl -fsS -m 10 -X POST "$ENDPOINT" \
        -H 'Content-Type: application/json' \
        -H "X-Gitea-Signature: $sig" \
        -d "$body" >/dev/null || echo "vault-mcp notify failed (non-fatal)" >&2
done
