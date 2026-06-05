#!/bin/bash
# model_manager.sh — Model loader/unloader for mobile AI node (Termux)
# Manages loading and unloading of models in llama.cpp to maximize inference speed
# while respecting RAM constraints on mobile.
#
# Usage:
#   model_manager.sh load <model_name>    # Load a model into memory
#   model_manager.sh unload <model_name>  # Unload a model from memory
#   model_manager.sh status               # Show current model state
#   model_manager.sh primary              # Load primary model (checks workstation first)
#
# Models:
#   primary  — 3-4B class open-source model (e.g. Qwen 3-4B) for cron/classification
#   qwen     — Qwen 3.5 9B Q4_K_M for general chat/reasoning

set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────────────────
# Edit these paths for your setup
MODEL_DIR="/data/data/com.termux/files/home/storage/shared/AI_Models"
LLAMA_BIN="/data/data/com.termux/files/home/llama.cpp/build-opencl/bin/llama-server"
STATE_FILE="/data/data/com.termux/files/home/.model_manager_state"
LOG_FILE="/data/data/com.termux/files/home/.model_manager.log"

# VPS health endpoint — set to your VPS Tailscale IP
VPS_HEALTH_URL="http://YOUR_VPS_TAILSCALE_IP:9191/mobile"

# ─── Model Definitions ──────────────────────────────────────────────────────
declare -A MODEL_PATHS
MODEL_PATHS=(
    ["primary"]="${MODEL_DIR}/Qwen3-4B-Q4_K_M.gguf"
    ["qwen"]="${MODEL_DIR}/Qwen3.5-9B-Q4_K_M.gguf"
)

# Model parameters — tune for your device
declare -A MODEL_THREADS
MODEL_THREADS=(
    ["primary"]=6
    ["qwen"]=6
)

declare -A MODEL_CTX
MODEL_CTX=(
    ["primary"]=4096
    ["qwen"]=4096
)

declare -A MODEL_NGL
MODEL_NGL=(
    ["primary"]=20
    ["qwen"]=30
)

# Server ports — each model gets its own port
declare -A MODEL_PORTS
MODEL_PORTS=(
    ["primary"]=8081
    ["qwen"]=8082
)

# ─── Functions ───────────────────────────────────────────────────────────────

log() {
    echo "[$(date '+%H:%M:%S')] $*" >> "$LOG_FILE"
    echo "$*"
}

get_loaded_models() {
    if [ -f "$STATE_FILE" ]; then
        cat "$STATE_FILE"
    else
        echo "{}"
    fi
}

is_loaded() {
    local model="$1"
    local state
    state=$(get_loaded_models)
    echo "$state" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('${model}',{}).get('loaded',False))" 2>/dev/null || echo "False"
}

is_model_available() {
    local model_path="${MODEL_PATHS[$1]:-}"
    if [ -z "$model_path" ]; then
        log "ERROR: Unknown model '$1'"
        return 1
    fi
    if [ ! -f "$model_path" ]; then
        log "ERROR: Model file not found: $model_path"
        return 1
    fi
    return 0
}

wait_for_server() {
    local port="$1"
    local max_wait="${2:-30}"
    local waited=0
    while [ $waited -lt $max_wait ]; do
        if curl -s "http://127.0.0.1:${port}/health" > /dev/null 2>&1; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

load_model() {
    local model="$1"

    if ! is_model_available "$model"; then
        return 1
    fi

    if [ "$(is_loaded "$model")" = "True" ]; then
        log "Model '$model' already loaded on port ${MODEL_PORTS[$model]}"
        return 0
    fi

    local model_path="${MODEL_PATHS[$model]}"
    local port="${MODEL_PORTS[$model]}"
    local threads="${MODEL_THREADS[$model]}"
    local ctx="${MODEL_CTX[$model]}"
    local ngl="${MODEL_NGL[$model]}"

    log "Loading '$model' on port $port (threads=$threads, ctx=$ctx, ngl=$ngl)..."

    # Use screen for reliable daemonization on Termux
    screen -dmS "llama-${model}" bash -c "
        export LD_LIBRARY_PATH=/vendor/lib64:/system_ext/lib64:/data/data/com.termux/files/usr/lib:\$LD_LIBRARY_PATH
        ${LLAMA_BIN} \
            --model '${model_path}' \
            --port ${port} \
            --host 127.0.0.1 \
            --threads ${threads} \
            --ctx-size ${ctx} \
            --n-gpu-layers ${ngl} \
            --chat-template chatml \
            --log-disable \
            > /dev/null 2>&1
    "

    # Wait for server to be ready
    if wait_for_server "$port" 60; then
        local pid
        pid=$(pgrep -f "llama-server.*${port}" | head -1)
        # Update state
        local state
        state=$(get_loaded_models)
        echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['${model}'] = {'loaded': True, 'pid': ${pid:-0}, 'port': $port, 'loaded_at': '$(date -Iseconds)'}
print(json.dumps(d, indent=2))
" > "$STATE_FILE"
        log "Model '$model' loaded successfully (PID=$pid, port=$port)"
    else
        log "ERROR: Model '$model' failed to start within 60s"
        return 1
    fi
}

unload_model() {
    local model="$1"

    if [ "$(is_loaded "$model")" != "True" ]; then
        log "Model '$model' is not loaded"
        return 0
    fi

    log "Unloading '$model'..."

    # Kill via screen session
    screen -X -S "llama-${model}" quit 2>/dev/null || true

    # Also kill any remaining process
    local pid
    pid=$(pgrep -f "llama-server.*${MODEL_PORTS[$model]}" | head -1)
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null || true
        sleep 2
        kill -9 "$pid" 2>/dev/null || true
    fi

    # Update state
    local state
    state=$(get_loaded_models)
    echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if '${model}' in d:
    del d['${model}']
print(json.dumps(d, indent=2))
" > "$STATE_FILE"

    log "Model '$model' unloaded"
}

load_primary() {
    # Check if workstation is online before loading
    local workstation_online="false"

    if command -v curl > /dev/null 2>&1; then
        workstation_online=$(curl -s --max-time 3 "$VPS_HEALTH_URL" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('workstation_online','false'))" 2>/dev/null || echo "false")
    fi

    if [ "$workstation_online" = "True" ] || [ "$workstation_online" = "true" ]; then
        log "Workstation is ONLINE — models stay unloaded"
        return 0
    fi

    log "Workstation OFFLINE — loading primary model for local inference"
    load_model "primary"
}

show_status() {
    local state
    state=$(get_loaded_models)
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')

    echo "=== Model Manager Status — $timestamp ==="
    echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if not d:
    print('  No models loaded')
for name, info in d.items():
    status = 'LOADED' if info.get('loaded') else 'unloaded'
    port = info.get('port', '?')
    pid = info.get('pid', '?')
    loaded_at = info.get('loaded_at', '?')
    print(f'  {name}: {status} (port={port}, pid={pid}, since={loaded_at})')
" 2>/dev/null || echo "  No models loaded"

    echo ""
    echo "=== Memory ==="
    free -h 2>/dev/null | head -2 || echo "  (free not available)"
}

# ─── Main ────────────────────────────────────────────────────────────────────

case "${1:-help}" in
    load)
        load_model "${2:-}"
        ;;
    unload)
        unload_model "${2:-}"
        ;;
    status)
        show_status
        ;;
    primary)
        load_primary
        ;;
    *)
        echo "Usage: $0 {load|unload|status|primary} [model]"
        echo "  models: primary, qwen"
        echo ""
        echo "Commands:"
        echo "  load <model>    Load a model into RAM and start llama-server"
        echo "  unload <model>  Stop llama-server and free model from RAM"
        echo "  status          Show current loaded models and RAM usage"
        echo "  primary         Load primary model (checks workstation availability first)"
        ;;
esac
