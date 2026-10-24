#!/usr/bin/env bash
# gx10.sh: reach the team's NVIDIA GX10 (GB10, Ollama) from a laptop without changing anything on the GX10.
#
# The GX10's Ollama listens on 127.0.0.1:11434 only and the machine is shared, so we never reconfigure it.
# Instead an SSH tunnel over Tailscale maps it to 127.0.0.1:11435 on the laptop, and Flock's `local` provider
# points there. Flock then only ever talks to loopback, so FLOCK_AIRGAP=1 passes (see flock/llm.py).
#
#   scripts/gx10.sh tunnel              # keep a tunnel up (reconnects on drop); Ctrl-C to stop
#   eval "$(scripts/gx10.sh env)"       # point Flock's judges, writer and mapper at the tunnel
#   eval "$(scripts/gx10.sh env --airgap)"   # same + FLOCK_AIRGAP=1 and a local reviewer: nothing leaves the network
#   scripts/gx10.sh status              # tailscale line, tunnel up?, models on disk, models loaded in memory
#   scripts/gx10.sh pull small          # ollama pull ON THE GX10, in the background (nohup), log ~/flock-pulls.log
#   scripts/gx10.sh pull large
#   scripts/gx10.sh log                 # last lines of the GX10's ~/flock-pulls.log
#
# Presets:  small = qwen3:8b gemma3:12b llama3.1:8b      large = gpt-oss:20b qwen3:30b gemma3:27b      all = both
#
# Env knobs (defaults in brackets):
#   GX10_HOST [gx10]  ssh host / alias (~/.ssh/config)     GX10_PORT [11435]  laptop port for the tunnel
#   GX10_REMOTE_PORT [11434]  Ollama's port on the GX10    GX10_RETRY [3]     seconds between reconnects
#   GX10_JUDGE_MODELS [gpt-oss:20b,qwen3:30b,gemma3:27b]  one per judge (strict, operator, skeptic)
#   GX10_MODEL [first judge model]  writer / mapper (and reviewer with --airgap)
#
# Needs: ssh, curl, python3 (for pretty status output). Plain bash, no other dependencies.
set -u

HOST="${GX10_HOST:-gx10}"
PORT="${GX10_PORT:-11435}"
REMOTE_PORT="${GX10_REMOTE_PORT:-11434}"
RETRY="${GX10_RETRY:-3}"
JUDGE_MODELS="${GX10_JUDGE_MODELS:-gpt-oss:20b,qwen3:30b,gemma3:27b}"
MODEL="${GX10_MODEL:-${JUDGE_MODELS%%,*}}"
URL="http://127.0.0.1:${PORT}"
PULL_LOG='~/flock-pulls.log'

PRESET_SMALL="qwen3:8b gemma3:12b llama3.1:8b"
PRESET_LARGE="gpt-oss:20b qwen3:30b gemma3:27b"

say() { printf '[gx10] %s\n' "$*" >&2; }

usage() {
    sed -n '2,/^set -u/p' "$0" | sed -e '/^set -u/d' -e 's/^# \{0,1\}//'
}

tunnel_up() {
    curl -s -m 3 "${URL}/api/version" 2>/dev/null
}

cmd_tunnel() {
    if v=$(tunnel_up) && [ -n "$v" ]; then
        say "something already answers on ${URL} (${v}); not starting another tunnel"
        return 0
    fi
    ssh_pid=""
    stop() {
        [ -n "$ssh_pid" ] && kill "$ssh_pid" 2>/dev/null
        say "tunnel stopped"
        exit 0
    }
    trap stop INT TERM
    while :; do
        say "connecting: 127.0.0.1:${PORT} -> ${HOST}:127.0.0.1:${REMOTE_PORT}"
        ssh -N -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
            -o ConnectTimeout=10 -L "${PORT}:127.0.0.1:${REMOTE_PORT}" "$HOST" &
        ssh_pid=$!
        i=0
        while kill -0 "$ssh_pid" 2>/dev/null && [ "$i" -lt 20 ]; do
            if v=$(tunnel_up) && [ -n "$v" ]; then
                say "tunnel UP: ${URL} -> Ollama on ${HOST} ${v}"
                say "next: eval \"\$($0 env)\" && python scripts/llm_check.py"
                break
            fi
            sleep 0.5
            i=$((i + 1))
        done
        if kill -0 "$ssh_pid" 2>/dev/null && [ "$i" -ge 20 ]; then
            say "ssh is connected but Ollama does not answer through it yet (is ollama running on ${HOST}?)"
        fi
        wait "$ssh_pid"
        code=$?
        ssh_pid=""
        say "tunnel DOWN (ssh exit ${code}); reconnecting in ${RETRY}s (Ctrl-C to stop)"
        sleep "$RETRY"
    done
}

cmd_env() {
    cat <<EOF
# Flock -> GX10 Ollama through the SSH tunnel ($0 tunnel)
export FLOCK_LOCAL_URL=${URL}/v1
export FLOCK_LOCAL_MODEL=${MODEL}
export FLOCK_JUDGE_PROVIDERS=local,local,local
export FLOCK_JUDGE_MODELS=${JUDGE_MODELS}
export FLOCK_WRITER_PROVIDER=local
export FLOCK_MAPPER_PROVIDER=local
export FLOCK_LLM_TIMEOUT=\${FLOCK_LLM_TIMEOUT:-120}
unset FLOCK_OFFLINE
EOF
    if [ "${1:-}" = "--airgap" ]; then
        cat <<EOF
export FLOCK_REVIEWER_PROVIDER=local
export FLOCK_AIRGAP=1
EOF
    fi
}

cmd_status() {
    ts=$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)
    # the ssh alias's real address (read from ~/.ssh/config, no connection made), matched by IP or name
    addr=$(ssh -G "$HOST" 2>/dev/null </dev/null | awk '$1 == "hostname" {print $2; exit}')
    if [ -x "$ts" ]; then
        line=$("$ts" status 2>/dev/null | awk -v h="$HOST" -v a="${addr:-$HOST}" \
            '$1 == a || $2 == a || $2 == h || index($2, a".") == 1 {print; exit}')
        echo "tailscale: ${line:-${HOST} (${addr:-?}) not found in 'tailscale status' (logged in? same tailnet?)}"
    else
        echo "tailscale: CLI not found"
    fi
    if ! v=$(tunnel_up) || [ -z "$v" ]; then
        echo "tunnel:    DOWN (nothing answers on ${URL}); start it with: $0 tunnel"
        return 1
    fi
    echo "tunnel:    UP ${URL} ${v}"
    tags=$(curl -s -m 10 "${URL}/api/tags")
    ps=$(curl -s -m 10 "${URL}/api/ps")
    TAGS="$tags" PS="$ps" python3 - <<'EOF'
import json, os

def load(name):
    try:
        return json.loads(os.environ[name] or "{}").get("models") or []
    except ValueError:
        return None

gb = lambda n: f"{(n or 0) / 1e9:.1f} GB"
tags, ps = load("TAGS"), load("PS")
if tags is None:
    print("models:    could not read /api/tags")
else:
    print(f"models:    {len(tags)} on disk")
    for m in sorted(tags, key=lambda m: m.get("name", "")):
        d = m.get("details") or {}
        print(f"  {m.get('name', '?'):<28} {gb(m.get('size')):>9}  {d.get('parameter_size', ''):>7} {d.get('quantization_level', '')}")
if ps is None:
    print("loaded:    could not read /api/ps")
else:
    print(f"loaded:    {len(ps)} in memory")
    for m in ps:
        print(f"  {m.get('name', '?'):<28} {gb(m.get('size')):>9}  vram {gb(m.get('size_vram'))}  until {m.get('expires_at', '?')[:19]}")
EOF
}

cmd_pull() {
    case "${1:-}" in
        small) models="$PRESET_SMALL" ;;
        large) models="$PRESET_LARGE" ;;
        all)   models="$PRESET_SMALL $PRESET_LARGE" ;;
        *) say "usage: $0 pull small|large|all   (small: ${PRESET_SMALL}; large: ${PRESET_LARGE})"; return 2 ;;
    esac
    say "starting on ${HOST} in the background: ollama pull ${models}"
    # One remote shell, detached with nohup: the pulls keep going if this laptop disconnects.
    ssh -n -o ConnectTimeout=10 "$HOST" "nohup sh -c 'for m in ${models}; do
            echo \"== \$(date -u +%FT%TZ) pulling \$m\"
            ollama pull \"\$m\" && echo \"== done \$m\" || echo \"== FAILED \$m\"
        done; echo \"== \$(date -u +%FT%TZ) preset ${1} finished\"' >> ${PULL_LOG} 2>&1 < /dev/null &" || return 1
    say "running. watch it: $0 log   (or: ssh ${HOST} tail -f ${PULL_LOG})"
}

cmd_log() {
    ssh -n -o ConnectTimeout=10 "$HOST" "tail -n ${1:-20} ${PULL_LOG}"
}

case "${1:-help}" in
    tunnel) cmd_tunnel ;;
    env)    shift; cmd_env "$@" ;;
    status) cmd_status ;;
    pull)   shift; cmd_pull "$@" ;;
    log)    shift; cmd_log "$@" ;;
    help|-h|--help) usage ;;
    *) say "unknown command: $1"; usage; exit 2 ;;
esac
