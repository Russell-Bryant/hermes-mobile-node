#!/bin/bash
# phone-tunnel-keepalive.sh — Ensure SSH tunnel to phone llama-server is always up
# Called by cron every 5 minutes
#
# Configuration: Edit these variables for your setup
#   PHONE_SSH_PORT     — SSH port on phone (default: 8022 for Termux)
#   PHONE_TAILSCALE_IP — Phone's Tailscale IP (e.g., 100.x.x.x)
#   SSH_KEY_PATH       — Path to SSH private key
#   VPS_USER           — Your VPS username (for SSH key path)

LOCAL_PORT=18081
REMOTE_PORT=8081
PHONE_SSH_PORT=8022
PHONE_TAILSCALE_IP="YOUR_PHONE_TAILSCALE_IP"  # e.g., 100.x.x.x
SSH_KEY_PATH="$HOME/.ssh/id_ed25519"  # Change to your SSH key path

# Check if tunnel is already listening
if ss -tlnp | grep -q ":${LOCAL_PORT}"; then
    # Verify it actually works
    if curl -s --connect-timeout 3 http://127.0.0.1:${LOCAL_PORT}/health >/dev/null 2>&1; then
        exit 0  # Tunnel is healthy
    fi
fi

# Tunnel is down or unhealthy — restart it
echo "[$(date)] Tunnel down, restarting..." >> /tmp/phone-tunnel.log

# Kill any stale SSH tunnel processes
pkill -f "ssh.*${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" 2>/dev/null
sleep 2

# Start new tunnel
ssh -i "${SSH_KEY_PATH}" \
    -o StrictHostKeyChecking=no \
    -o BatchMode=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -L ${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT} \
    -p ${PHONE_SSH_PORT} \
    ${PHONE_TAILSCALE_IP} \
    "while true; do sleep 300; done" &

sleep 5

# Verify
if curl -s --connect-timeout 5 http://127.0.0.1:${LOCAL_PORT}/health >/dev/null 2>&1; then
    echo "[$(date)] Tunnel restarted successfully" >> /tmp/phone-tunnel.log
else
    echo "[$(date)] Tunnel restart FAILED" >> /tmp/phone-tunnel.log
fi
