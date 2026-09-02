#!/bin/sh
# Make the image usable under an arbitrary `user: <uid>:<gid>`.
#
# Two things break when a container runs as a UID with no /etc/passwd entry:
#
#   1. getpwuid() fails, so the OpenSSH client refuses to start ("You don't
#      exist, go away!") and git-over-SSH dies before it reaches the remote.
#   2. git refuses to touch a repo owned by a different UID ("detected dubious
#      ownership") unless that path is marked safe.
#
# We fix (1) with nss_wrapper, which synthesizes passwd/group entries via
# LD_PRELOAD rather than requiring a writable /etc/passwd, and (2) by writing a
# gitconfig into the state directory instead of $HOME, which may not be
# writable.
set -eu

STATE_DIR="${STATE_DIR:-/state}"
VAULT_PATH="${VAULT_PATH:-/vault}"

uid="$(id -u)"
gid="$(id -g)"

if ! mkdir -p "$STATE_DIR" 2>/dev/null || ! [ -w "$STATE_DIR" ]; then
    echo "entrypoint: $STATE_DIR is not writable by ${uid}:${gid}." >&2
    echo "entrypoint: mount a volume there, or chown it to the uid you run as." >&2
    exit 1
fi

# ---- 1. synthesize a passwd entry when the UID is unknown ------------------

if ! getent passwd "$uid" >/dev/null 2>&1; then
    wrapper=""
    for candidate in /usr/lib/*/libnss_wrapper.so /usr/lib/libnss_wrapper.so; do
        [ -f "$candidate" ] && wrapper="$candidate" && break
    done

    if [ -n "$wrapper" ]; then
        passwd_file="$STATE_DIR/passwd"
        group_file="$STATE_DIR/group"
        # Copy the base files so system accounts still resolve, then append.
        cp /etc/passwd "$passwd_file"
        cp /etc/group "$group_file"
        printf 'app:x:%s:%s:app:%s:/usr/sbin/nologin\n' "$uid" "$gid" "$HOME" >> "$passwd_file"
        if ! getent group "$gid" >/dev/null 2>&1; then
            printf 'app:x:%s:\n' "$gid" >> "$group_file"
        fi
        export LD_PRELOAD="$wrapper"
        export NSS_WRAPPER_PASSWD="$passwd_file"
        export NSS_WRAPPER_GROUP="$group_file"
    else
        echo "entrypoint: uid ${uid} is not in /etc/passwd and nss_wrapper is" >&2
        echo "entrypoint: unavailable; git over SSH will fail. Use an HTTPS" >&2
        echo "entrypoint: remote, or run as a uid that exists in the image." >&2
    fi
fi

# ---- 2. git identity and ownership ----------------------------------------

export GIT_CONFIG_GLOBAL="${GIT_CONFIG_GLOBAL:-$STATE_DIR/gitconfig}"
: > "$GIT_CONFIG_GLOBAL"
git config --global --add safe.directory "$VAULT_PATH"
git config --global --add safe.directory '*'

# ---- 3. HTTP(S) credentials -----------------------------------------------
#
# Gitea accepts an access token as the HTTP password with any username. The
# token goes in a 0600 credential file under $STATE_DIR rather than in the
# remote URL, so it stays out of .git/config, `git remote -v`, and the process
# list. GIT_TOKEN_FILE lets you feed it from a Docker/Compose secret instead of
# an environment variable.

if [ -n "${GIT_TOKEN_FILE:-}" ]; then
    if [ -r "$GIT_TOKEN_FILE" ]; then
        GIT_TOKEN="$(cat "$GIT_TOKEN_FILE")"
    else
        echo "entrypoint: GIT_TOKEN_FILE=$GIT_TOKEN_FILE is not readable." >&2
        exit 1
    fi
fi

if [ -n "${GIT_TOKEN:-}" ]; then
    remote_url="${GIT_REMOTE_URL:-}"
    if [ -z "$remote_url" ]; then
        remote_url="$(git -C "$VAULT_PATH" remote get-url "${GIT_REMOTE:-origin}" 2>/dev/null || true)"
    fi
    if [ -z "$remote_url" ]; then
        echo "entrypoint: GIT_TOKEN is set but no remote is known." >&2
        echo "entrypoint: set GIT_REMOTE_URL, or add the remote to the vault repo." >&2
        exit 1
    fi

    cred_file="$STATE_DIR/git-credentials"
    # Percent-encode both fields: tokens are usually URL-safe, usernames
    # containing '@' or ':' are not, and a raw one silently corrupts the entry.
    cred_line="$(
        REMOTE="$remote_url" \
        CRED_USER="${GIT_USERNAME:-oauth2}" \
        CRED_TOKEN="$GIT_TOKEN" \
        python3 -c '
import os, sys, urllib.parse as u
p = u.urlsplit(os.environ["REMOTE"])
if p.scheme not in ("http", "https"):
    sys.exit("entrypoint: GIT_TOKEN only applies to http(s) remotes, got " + p.scheme)
host = p.hostname or ""
if p.port:
    host += ":%d" % p.port
user = u.quote(os.environ["CRED_USER"], safe="")
tok = u.quote(os.environ["CRED_TOKEN"], safe="")
print("%s://%s:%s@%s" % (p.scheme, user, tok, host))
'
    )"

    ( umask 077; printf '%s\n' "$cred_line" > "$cred_file" )
    chmod 0600 "$cred_file"
    git config --global credential.helper "store --file=$cred_file"
    # Match on host, not on the full repo path, so one entry covers the repo
    # and any future ones on the same Gitea instance.
    git config --global credential.useHttpPath false
fi

# $HOME must exist for ssh to find ~/.ssh; warn rather than fail, since an
# HTTPS remote does not need it.
if [ -n "${HOME:-}" ] && [ ! -d "$HOME" ] && [ -z "${GIT_TOKEN:-}" ]; then
    echo "entrypoint: HOME=$HOME does not exist; SSH remotes will not work." >&2
fi

exec "$@"
