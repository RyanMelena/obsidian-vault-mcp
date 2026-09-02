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

# $HOME must exist for ssh to find ~/.ssh; warn rather than fail, since a
# vault synced over an HTTPS remote does not need it.
if [ -n "${HOME:-}" ] && [ ! -d "$HOME" ]; then
    echo "entrypoint: HOME=$HOME does not exist; SSH remotes will not work." >&2
fi

exec "$@"
