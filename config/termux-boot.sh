#!/bin/bash
# termux-boot.sh — Auto-start script for Termux boot
# Place this file at ~/.termux/boot/start-hermes-mobile.sh
# Make sure the boot directory exists: mkdir -p ~/.termux/boot
#
# Termux:Boot must be installed and launched at least once for this to work.

# Wait for network connectivity
echo "[$(date)] Hermes Mobile boot script starting..." >> /data/data/com.termux/files/home/.hermes_mobile_boot.log

# Wait up to 60s for WiFi
for i in $(seq 1 60); do
    if ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then
        echo "[$(date)] Network connected after ${i}s" >> /data/data/com.termux/files/home/.hermes_mobile_boot.log
        break
    fi
    sleep 1
done

# Wait an extra 10s for Tailscale to connect
sleep 10

# Start llama-server in screen session
echo "[$(date)] Starting llama-server..." >> /data/data/com.termux/files/home/.hermes_mobile_boot.log

screen -dmS llamaserve bash -c '
    export LD_LIBRARY_PATH=/vendor/lib64:/system_ext/lib64:/data/data/com.termux/files/usr/lib
    /data/data/com.termux/files/home/llama.cpp/build-opencl/bin/llama-server \
        -m /data/data/com.termux/files/home/storage/shared/AI_Models/Qwen3-4B-Q4_K_M.gguf \\
        -c 4096 \
        --host 127.0.0.1 \
        --port 8081 \
        --chat-template chatml \
        -t 6 \
        --gpu-layers 20 \
        >> /data/data/com.termux/files/home/llama-server.log 2>&1
'

# Wait for llama-server to be ready
sleep 10

# Start hermes_mobile.py daemon
echo "[$(date)] Starting hermes_mobile.py daemon..." >> /data/data/com.termux/files/home/.hermes_mobile_boot.log

screen -dmS hermesmobile bash -c '
    cd /data/data/com.termux/files/home
    python3 /data/data/com.termux/files/home/.hermes/scripts/hermes_mobile.py --daemon \
        >> /data/data/com.termux/files/home/.hermes_mobile.log 2>&1
'

echo "[$(date)] Hermes Mobile boot complete" >> /data/data/com.termux/files/home/.hermes_mobile_boot.log
