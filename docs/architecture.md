# Architecture

## Overview

The Hermes Mobile Node extends a self-hosted AI agent (Hermes) with a phone-based inference failover. The phone runs `llama.cpp` locally and serves an OpenAI-compatible API, which the VPS accesses through an SSH tunnel.

## System Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                        MESSAGING LAYER                                │
│   Telegram ──┐                                                       │
│   Discord  ──┤                                                       │
│   Signal   ──┘                                                       │
└──────────────────────────┬───────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          VPS (Ubuntu)                                 │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │                    Hermes Agent Gateway                      │     │
│  │                                                              │     │
│  │  model: qwen/qwen3.6-27b (LM Studio, primary)               │     │
│  │  providers:                                                  │     │
│  │    phone: http://127.0.0.1:18081/v1  ← SSH tunnel           │     │
│  │  fallback: openrouter/owl-alpha                              │     │
│  └──────────┬──────────────────┬──────────────────┬─────────────┘     │
│             │                  │                  │                    │
│             │           ┌──────┴──────┐           │                    │
│             │           │  cron_relay  │           │                    │
│             │           │  (3-tier     │           │                    │
│             │           │   routing)   │           │                    │
│             │           └──────┬──────┘           │                    │
│             │                  │                  │                    │
│  ┌──────────┴──────────┐      │      ┌───────────┴──────────┐        │
│  │  workstation_health  │      │      │  phone-tunnel-       │        │
│  │  endpoint (:9191)    │      │      │  keepalive.sh        │        │
│  │  /health + /mobile   │      │      │  (cron every 5 min)  │        │
│  └──────────┬──────────┘      │      └───────────┬──────────┘        │
│             │                  │                  │                    │
└─────────────┼──────────────────┼──────────────────┼────────────────────┘
              │                  │                  │
              │  Tailscale       │  SSH tunnel      │  SSH tunnel
              │  health check    │  -L 18081        │  -L 18081
              │                  │  :127.0.0.1:8081 │  :127.0.0.1:8081
              │                  │                  │
              ▼                  ▼                  ▼
┌─────────────────┐  ┌─────────────────────────────────────────────┐
│   WORKSTATION    │  │              PHONE (Android + Termux)       │
│                  │  │                                              │
│  LM Studio       │  │  ┌──────────────────────────────────────┐   │
│  Qwen 27B       │  │  │         llama-server (:8081)          │   │
│  port 1235      │  │  │                                       │   │
│                  │  │  │  Model: Nemotron 3 Nano 4B Q4_K_M    │   │
│  Tier 1:        │  │  │  Context: 4096                        │   │
│  PRIMARY        │  │  │  Backend: OpenCL (Adreno 830)         │   │
│                  │  │  │  Threads: 6                           │   │
│                  │  │  └──────────────────────────────────────┘   │
│                  │  │                                              │
│                  │  │  ┌──────────────────────────────────────┐   │
│                  │  │  │      hermes_mobile.py (:9192)        │   │
│                  │  │  │                                       │   │
│                  │  │  │  /health — node status               │   │
│                  │  │  │  /workstation — check VPS            │   │
│                  │  │  │  /infer — local inference            │   │
│                  │  │  │  /cron/trigger — run cron job        │   │
│                  │  │  │  /model/load — load model in RAM     │   │
│                  │  │  │  /model/unload — free RAM            │   │
│                  │  │  └──────────────────────────────────────┘   │
│                  │  │                                              │
│                  │  │  ┌──────────────────────────────────────┐   │
│                  │  │  │    mobile_model_manager.sh           │   │
│                  │  │  │                                       │   │
│                  │  │  │  load/unload models on demand        │   │
│                  │  │  │  RAM management (24GB shared)        │   │
│                  │  │  └──────────────────────────────────────┘   │
│                  │  │                                              │
│                  │  │  Tier 2: FAILOVER                            │
│                  │  │                                              │
└─────────────────┘  └─────────────────────────────────────────────┘
                                          │
                                          │ (if both Tier 1 + 2 fail)
                                          ▼
                               ┌──────────────────┐
                               │   OpenRouter     │
                               │   OWL-Alpha      │
                               │                  │
                               │   Tier 3: CLOUD  │
                               └──────────────────┘
```

## Three-Tier Routing Logic

```
1. Check workstation health via http://localhost:9191/mobile
   │
   ├─ Workstation ONLINE ──→ Run job on LM Studio (Tier 1)
   │
   └─ Workstation OFFLINE
      │
      ├─ Phone reachable via Tailscale ──→ Forward to phone (Tier 2)
      │                                     SSH tunnel: VPS:18081 → phone:8081
      │                                     or HTTP: phone:9192/cron/trigger
      │
      └─ Phone unreachable ──→ Call OpenRouter API (Tier 3)
                                Model: openrouter/owl-alpha
```

## SSH Tunnel Pattern

The core networking pattern is an SSH reverse tunnel from VPS to phone:

```
VPS (Ubuntu)                      Phone (Termux)
┌─────────────────┐              ┌─────────────────┐
│                 │   SSH tunnel │                 │
│  Hermes Gateway │◄─────────────│  llama-server   │
│                 │  -L 18081    │  port 8081      │
│  provider:      │  :127.0.0.1  │  (localhost     │
│    phone        │  :8081       │   only)         │
│  base_url:      │              │                 │
│    127.0.0.1    │              │                 │
│    :18081/v1    │              │                 │
└─────────────────┘              └─────────────────┘
```

The tunnel is kept alive by:
- `ServerAliveInterval=30` — SSH keepalive pings
- `phone-tunnel-keepalive.sh` — cron job every 5 min checks tunnel health
- Remote `while true; do sleep 300; done` — keeps SSH session open

## Data Flow — Cron Job Routing

```
Cron fires on VPS
       │
       ▼
cron_relay.py checks workstation_health.py
       │
       ├─ Workstation OK ──→ Run normally (LM Studio)
       │
       ├─ Workstation down, phone up ──→ POST to phone:9192/cron/trigger
       │                                  with job name + context
       │                                  (2000 char context limit)
       │
       └─ Both down ──→ POST to OpenRouter API
                         with full system prompt
                         (no tool access, context-only)
```

## Data Flow — Telegram Chat

```
User sends message in Telegram
       │
       ▼
Hermes Gateway receives message
       │
       ├─ Default model (LM Studio) ──→ Qwen 27B generates response
       │
       ├─ User switches to phone model ──→ Nemotron 4B generates response
       │                                    via SSH tunnel
       │
       └─ Fallback (if primary down) ──→ Phone or OpenRouter
```

## Phone Service Layer (hermes_mobile.py)

The phone runs a lightweight HTTP service that:

1. **Health monitoring** — reports node status, model availability, workstation connectivity
2. **Model management** — loads/unloads GGUF models based on demand (RAM is finite at 24GB shared)
3. **Cron relay target** — receives cron job triggers from VPS when workstation is offline
4. **Inference proxy** — forwards requests to llama-server with appropriate model selection

### Model Manager

The model manager handles RAM-constrained inference:

- **Nemotron 4B** (~2.7GB) — default for cron jobs, light chat
- **Qwen 3.5 9B** (~5.3GB) — available for heavier tasks if RAM allows
- Models are loaded on demand and unloaded when idle or when workstation comes back online

## Security Considerations

- **llama-server binds to 127.0.0.1 only** — not exposed to the network directly
- **SSH tunnel is the only access path** — key-based auth, no passwords
- **Tailscale for transport** — encrypted mesh network, no open ports
- **No cloud API keys on phone** — fully local inference
- **Phone service layer** also binds to localhost by default (cron relay reaches it via Tailscale)

## Network Topology

```
Internet
    │
    ├── Telegram/Discord APIs
    │
    └── VPS (public IP)
         │
         ├── Tailscale mesh (100.x.x.x)
         │    ├── Workstation (100.x.x.x (Tailscale))
         │    └── Phone (100.x.x.x (Tailscale))
         │
         └── SSH tunnel (local)
              └── VPS:18081 → Phone:8081
```

All inter-node communication goes through Tailscale. The SSH tunnel is only between VPS localhost and phone localhost — it never crosses the public internet unencrypted.
