# Network Diagnostics

## Phone Unreachable from VPS

### Step 1: Check Tailscale (Phone Side)

The phone has NO Tailscale CLI — must check the Android app UI.

1. Open Tailscale app on phone
2. Verify it shows "Connected"
3. If not connected, re-authenticate via Chrome at `https://login.tailscale.com`

### Step 2: Check WiFi (Phone Side)

```bash
# In Termux
ip addr show wlan0 2>/dev/null | grep 'inet ' || ip addr show | grep 'inet '
```

**Note:** `ip route show default` may return `"INET (IPv4) not configured in this system"` — this is Termux's netstat wrapper when WiFi is off. Not a real error.

### Step 3: Check SSH Daemon (Phone Side)

```bash
pgrep -f sshd && echo "sshd running" || echo "sshd NOT running"
```

If not running: `/usr/bin/sshd`

### Step 4: Check from VPS Side

```bash
# Ping phone via Tailscale
ping YOUR_PHONE_TAILSCALE_IP

# Check Tailscale status
tailscale status --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
for p in d.get('Peer', {}).values():
    if 'YOUR_PHONE_TAILSCALE_IP' in p.get('TailscaleIPs', []):
        print(p['HostName'], p['TailscaleIPs'], p['Online'])
"
```

### Common Failure Modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Connection timed out` | Phone not on Tailscale | Open Tailscale app, connect |
| `Connection refused` | SSH daemon dead | `pkill -f sshd && /usr/bin/sshd` |
| `No route to host` | Phone asleep | Wake phone, check Tailscale |
| Ping works, SSH fails | SSH daemon crashed | Restart sshd on phone |
| Tailscale won't connect | Auth expired | Re-authenticate via browser |

## SSH Tunnel Down on VPS

### Diagnose

```bash
# Is the tunnel process running?
ss -tlnp | grep 18081

# Is the SSH process alive?
ps aux | grep "ssh.*18081" | grep -v grep

# Does the tunnel respond?
curl -s --connect-timeout 5 http://127.0.0.1:18081/health
```

### Fix

```bash
# Kill stale tunnel
pkill -f "ssh.*18081.*8081" 2>/dev/null
sleep 2

# Restart tunnel
ssh -i ~/.ssh/id_ed25519 \
  -o StrictHostKeyChecking=no -o BatchMode=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -L 18081:127.0.0.1:8081 -p 8022 \
  YOUR_PHONE_TAILSCALE_IP "while true; do sleep 300; done" &

# Verify
sleep 5
curl -s http://127.0.0.1:18081/health
```

## llama-server Not Responding on Phone

### Diagnose

```bash
# From VPS, SSH into phone
ssh -p 8022 YOUR_PHONE_TAILSCALE_IP "curl -s http://127.0.0.1:8081/health"

# Check if llama-server is running
ssh -p 8022 YOUR_PHONE_TAILSCALE_IP "pgrep -f llama-server"

# Check screen sessions
ssh -p 8022 YOUR_PHONE_TAILSCALE_IP "screen -ls"
```

### Fix

```bash
# Kill and restart llama-server
ssh -p 8022 YOUR_PHONE_TAILSCALE_IP "pkill -f llama-server; sleep 2"

ssh -p 8022 YOUR_PHONE_TAILSCALE_IP "screen -dmS llamaserve bash -c '
  export LD_LIBRARY_PATH=/vendor/lib64:/system_ext/lib64:/data/data/com.termux/files/usr/lib
  ~/llama.cpp/build-opencl/bin/llama-server \\
    -m ~/storage/shared/AI_Models/Qwen3-4B-Q4_K_M.gguf \\
    -c 4096 --host 127.0.0.1 --port 8081 \\
    --chat-template chatml -t 6 --gpu-layers 20
'"

# Verify
sleep 5
ssh -p 8022 YOUR_PHONE_TAILSCALE_IP "curl -s http://127.0.0.1:8081/health"
```

## Model Download Issues

### Resume Interrupted Download

```bash
# curl -C - resumes from where it left off
curl -C - -L \
  "https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf" \
  -o ~/storage/shared/AI_Models/Qwen3-4B-Q4_K_M.gguf
```

**Note:** If the file is already complete, curl exits 0 with "Download complete". Verify with `stat -c%s` (exact bytes), not `ls -lh` (rounds down).

### Verify GGUF Integrity

```bash
# Check GGUF magic number (first 4 bytes should be 0x47475546 = "GGUF")
xxd -l 4 ~/storage/shared/AI_Models/Qwen3-4B-Q4_K_M.gguf
# Expected output: 00000000: 4747 5546                                GGUF
```

### Alternative: Download on VPS, Transfer via SCP

If phone internet is slow or HuggingFace is blocked:

```bash
# On VPS — download with Python
python3 -c "
import urllib.request
url = 'https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf'
urllib.request.urlretrieve(url, '/tmp/model.gguf')
"

# Transfer to phone
scp -o ConnectTimeout=30 -P 8022 /tmp/model.gguf YOUR_PHONE_TAILSCALE_IP:~/storage/shared/AI_Models/
```

## Performance Issues

### Slow Inference

```bash
# Check if GPU (OpenCL) is being used
# On phone, check llama-server logs:
ssh -p 8022 YOUR_PHONE_TAILSCALE_IP "tail -50 ~/llama-server.log | grep -i opencl"

# If falling back to CPU, rebuild with OpenCL:
cd ~/llama.cpp
git checkout c20c44514
cmake -B build-opencl -DGGML_OPENCL=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build-opencl -j$(nproc) --config Release
```

### Out of Memory

```bash
# Check available RAM on phone
ssh -p 8022 YOUR_PHONE_TAILSCALE_IP "free -h"

# Reduce context length (edit llama-server args)
# -c 2048 instead of -c 4096

# Reduce GPU layers
# --gpu-layers 10 instead of --gpu-layers 20
```

## SSH Backgrounding Issues

**Critical:** The Hermes SSH backend intercepts shell-level backgrounding (`&`, `nohup`, `disown`, `setsid`). Background processes are killed immediately.

**Use `screen` instead:**

```bash
# Start a daemon in screen
screen -dmS myservice bash -c 'command here'

# List screen sessions
screen -ls

# Attach to a session
screen -r myservice

# Kill a session
screen -X -S myservice quit
```

**For auto-start on phone boot:**
```bash
# Add to Termux crontab
crontab -e
# Add: @reboot sleep 30 && screen -dmS llamaserve bash -c '...'
```

## llama.cpp Build Issues on Android

### Latest main is broken (June 2026)

The latest `llama.cpp` main branch segfaults on Android (both Vulkan and CPU-only). **Fix: checkout an older commit:**

```bash
cd ~/llama.cpp
git checkout c20c44514
rm -rf build-opencl build
```

### Vulkan crashes on load

Vulkan crashes with tensor allocation errors on Adreno 830. **Use OpenCL instead:**

```bash
cmake -B build-opencl -DGGML_OPENCL=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build-opencl -j$(nproc) --config Release
```

Set `LD_LIBRARY_PATH` when running:
```bash
LD_LIBRARY_PATH=/vendor/lib64:/system_ext/lib64:/data/data/com.termux/files/usr/lib:$LD_LIBRARY_PATH \
  ./build-opencl/bin/llama-server ...
```
