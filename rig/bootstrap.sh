#!/usr/bin/env bash
# clorqub rig bootstrap — runs as the Clore order `command`.
# Locks down the cloreai/hiveos image defaults, starts key-only sshd,
# fetches + verifies the agent, then execs it.
#   arg 1: repo commit sha to fetch rig files from
#   env:   CLORQUB_REPO_RAW (default https://raw.githubusercontent.com/dcogswell/qub-rig)
#          CLORQUB_SSH_PUBKEY  (public key line, optional)
#          CLORQUB_LOCAL_SRC   (dir containing rig/ — copy instead of download;
#                             for local testing only)
set -u

COMMIT="${1:?usage: bootstrap.sh <commit-sha>}"
REPO_RAW="${CLORQUB_REPO_RAW:-https://raw.githubusercontent.com/dcogswell/qub-rig}"
DEST=/clorqub/agent
LOG=/clorqub/bootstrap.log

mkdir -p "$DEST"
exec > >(tee -a "$LOG") 2>&1
ts() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
say() { echo "$(ts) [bootstrap] $*"; }
die() { say "FATAL: $*"; say "sleeping forever so the container stays up for SSH debugging"; sleep infinity; }

say "bootstrap start commit=$COMMIT repo=$REPO_RAW"

# --- lock down image defaults ------------------------------------------------
if id user >/dev/null 2>&1; then
    passwd -l user >/dev/null 2>&1 && say "locked password for 'user'" \
        || say "warn: passwd -l user failed"
fi

mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/90-clorqub.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
EOF
say "wrote sshd drop-in (key-only)"

ssh-keygen -A >/dev/null 2>&1 || die "ssh-keygen -A failed"

if [ -n "${CLORQUB_SSH_PUBKEY:-}" ]; then
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    printf '%s\n' "$CLORQUB_SSH_PUBKEY" >> /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    say "installed ssh pubkey for root"
else
    say "warn: no CLORQUB_SSH_PUBKEY; sshd will have no authorized keys"
fi

mkdir -p /run/sshd
/usr/sbin/sshd || die "sshd failed to start"
say "sshd started"

# --- fetch + verify agent files ----------------------------------------------
fetch() { # url dest
    local url="$1" dest="$2" i
    for i in 1 2 3 4 5; do
        if curl -fsSL --connect-timeout 15 --max-time 120 -o "$dest" "$url"; then
            return 0
        fi
        say "retry $i: $url"
        sleep $((i * 3))
    done
    return 1
}

if [ -n "${CLORQUB_LOCAL_SRC:-}" ]; then
    say "CLORQUB_LOCAL_SRC=$CLORQUB_LOCAL_SRC: copying rig files locally"
    cp "$CLORQUB_LOCAL_SRC/agent.py" "$CLORQUB_LOCAL_SRC/qubic_parse.py" \
       "$CLORQUB_LOCAL_SRC/SHA256SUMS" "$DEST/" \
        || die "local copy failed"
else
    for f in agent.py qubic_parse.py SHA256SUMS; do
        fetch "$REPO_RAW/$COMMIT/rig/$f" "$DEST/$f" \
            || die "download failed: $f"
    done
    say "downloaded agent files at $COMMIT"
fi

cd "$DEST" || die "cd $DEST failed"
sha256sum -c SHA256SUMS || die "SHA256SUMS verification failed"
say "agent files verified"

say "exec agent"
exec python3 "$DEST/agent.py" run
