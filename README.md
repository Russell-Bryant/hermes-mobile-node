# Hermes Mobile Node

Turn an Android phone into an AI inference failover node for a VPS + workstation setup. When your workstation goes offline, the phone picks up cron jobs and light conversational workloads via local LLM inference — no cloud APIs needed.

**Status:** Production-ready (confirmed June 2026, Snapdragon 8 Elite + Hermes Agent).

---

## Why

Self-hosted AI workflows break when the workstation goes down. Cloud APIs (OpenRouter, etc.) are the usual fallback, but they cost money, leak data, and require internet. A phone with a local GGUF model gives you:

- **Zero cloud dependency** for failover inference
- **Full data privacy** — nothing leaves the device
- **Near-zero cost** — the phone is already on
- **Fast enough** for cron jobs and light chat (~2.6 t/s on Adreno 830)

---

## Architecture

```
Telegram / Discord
       │
       ▼
  ┌─────────┐
  │   VPS    │  Hermes Agent (gateway)
  │  Ubuntu  │
  └────┬─────┘
       │
       ├── Tier 1: Workstation (LM Studio, Qwen 27B) ── primary
       │
       ├── Tier 2: Phone (llama.cpp, Nemotron 4B) ──── failover
       │           SSH tunnel: VPS:18081 → phone:8081
       │
       └── Tier 3: OpenRouter (OWL-Alpha) ───────────── cloud fallback
```

**Three-tier routing:**
1. Workstation online → run everything on the big local model
2. Workstation offline, phone reachable → forward to phone via SSH tunnel
3. Both unreachable → fall back to OpenRouter cloud API

---

## What's Included

| File | Purpose |
|------|---------|
| `scripts/phone-tunnel-keepalive.sh` | VPS-side SSH tunnel manager with auto-restart |
| `scripts/mobile-model-manager.sh` | Phone-side model loader/unloader (RAM management) |
| `scripts/hermes_mobile.py` | Phone-side service daemon (health, inference, cron trigger) |
| `scripts/workstation_health.py` | VPS health endpoint (workstation + phone + system status) |
| `scripts/cron_relay.py` | Three-tier cron job router |
| `config/hermes-provider-phone.yaml` | Hermes config snippet for phone provider |
| `config/termux-boot.sh` | Phone auto-start on boot |
| `docs/architecture.md` | Detailed architecture with diagrams |
| `docs/network-diagnostics.md` | Troubleshooting guide |

---

## Requirements

### Phone
- Android with Termux (any phone that runs llama.cpp)
- 8GB+ RAM recommended (24GB ideal)
- GPU: Adreno 830 (Snapdragon 8 Elite) for OpenCL acceleration
- llama.cpp built with OpenCL support
- GGUF model (Nemotron 3 Nano 4B Q4_K_M recommended)
- Tailscale (Android app) for stable VPS ↔ phone connectivity

### VPS
- Ubuntu (or any Linux)
- Hermes Agent installed
- SSH access to phone (key-based auth)
- Tailscale (CLI) for stable routing

### Workstation (optional, for 3-tier setup)
- LM Studio or any OpenAI-compatible API
- Tailscale for VPS health checks

---

## Quick Start

### 1. Phone — Build llama.cpp with OpenCL

```bash
# In Termux
pkg install -y git cmake opencl-headers opencl-icd-loader
git clone https://github.com/ggerganov/llama.cpp
cd llama.cpp
# IMPORTANT: checkout a known-good commit (latest main may segfault on Android)
git checkout c20c44514
cmake -B build-opencl -DGGML_OPENCL=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build-opencl -j$(nproc) --config Release
```

### 2. Phone — Download Model

```bash
# Install hf CLI
pip install huggingface_hub

# Download Nemotron 3 Nano 4B
mkdir -p ~/storage/shared/AI_Models
hf download nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF \
  NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf \
  --local-dir ~/storage/shared/AI_Models
```

### 3. Phone — Start llama-server

```bash
screen -dmS llamaserve bash -c '
  export LD_LIBRARY_PATH=/vendor/lib64:/system_ext/lib64:/data/data/com.termux/files/usr/lib
  ~/llama.cpp/build-opencl/bin/llama-server \
    -m ~/storage/shared/AI_Models/NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf \
    -c 4096 \
    --host 127.0.0.1 \
    --port 8081 \
    --chat-template chatml \
    -t 6 \
    --gpu-layers 20
'
```

### 4. VPS — Create SSH Tunnel

```bash
# One-shot tunnel (keeps SSH alive with a sleep loop)
ssh -i ~/.ssh/id_ed25519 \
  -o StrictHostKeyChecking=no \
  -o BatchMode=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -L 18081:127.0.0.1:8081 \
  -p 8022 \
  YOUR_PHONE_TAILSCALE_IP \
  "while true; do sleep 300; done" &

# Verify
curl -s http://127.0.0.1:18081/health
```

### 5. VPS — Add Phone as Hermes Provider

Add to `~/.hermes/config.yaml` (use Python — file is protected from direct edits):

```python
import yaml
cfg = yaml.safe_load(open('/home/YOUR_USER/.hermes/config.yaml'))
cfg['providers']['phone'] = {
    'base_url': 'http://127.0.0.1:18081/v1',
    'api_key': 'phone-local',
    'model': 'nvidia/NVIDIA-Nemotron3-Nano-4B-Q4_K_M',
    'context_length': 4096
}
cfg.setdefault('model_catalog', {}).setdefault('providers', {})['phone'] = {
    'base_url': 'http://127.0.0.1:18081/v1',
    'models': ['nvidia/NVIDIA-Nemotron3-Nano-4B-Q4_K_M']
}
with open('/home/YOUR_USER/.hermes/config.yaml', 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False)
```

### 6. VPS — Install Tunnel Keepalive

```bash
cp scripts/phone-tunnel-keepalive.sh ~/scripts/
chmod +x ~/scripts/phone-tunnel-keepalive.sh
(crontab -l 2>/dev/null; echo "*/5 * * * * /home/YOUR_USER/scripts/phone-tunnel-keepalive.sh >/dev/null 2>&1") | crontab -
```

### 7. Verify End-to-End

```bash
# Test inference through tunnel
curl -s http://127.0.0.1:18081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Say hello in 5 words"}],"max_tokens":32}'
```

---

## Switching to the Phone Model

Once the phone is a Hermes provider, you can use it directly in Telegram/Discord:

```
/model phone/nvidia/NVIDIA-Nemotron3-Nano-4B-Q4_K_M
```

Or set up automatic failover in your Hermes config so the phone kicks in when the workstation goes down.

---

## Performance

| Device | Model | Backend | Prompt | Generation |
|--------|-------|---------|--------|------------|
| Snapdragon 8 Elite (phone) | Nemotron 4B Q4 | OpenCL (Adreno 830) | ~9.4 t/s | ~2.6 t/s |
| Snapdragon 8 Elite (phone) | Nemotron 4B Q4 | CPU-only | ~20 t/s | ~20 t/s |
| RTX 3090 (workstation) | Qwen 27B Q4 | CUDA | ~45 t/s | ~12 t/s |

The phone is slower than a workstation GPU but fast enough for cron jobs and light chat. CPU-only mode on the Snapdragon 8 Elite is actually competitive for generation speed.

---

## Troubleshooting

See [docs/network-diagnostics.md](docs/network-diagnostics.md) for the full troubleshooting guide.

**Quick checks:**

```bash
# Is the phone reachable?
ping YOUR_PHONE_TAILSCALE_IP

# Is llama-server running on the phone?
ssh -p 8022 YOUR_PHONE_TAILSCALE_IP "curl -s http://127.0.0.1:8081/health"

# Is the tunnel up on the VPS?
curl -s http://127.0.0.1:18081/health

# Is the model responding?
curl -s http://127.0.0.1:18081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"test"}],"max_tokens":10}'
```

---

## License

MIT — see [LICENSE](LICENSE).
