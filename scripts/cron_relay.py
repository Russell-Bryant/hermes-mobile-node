#!/usr/bin/env python3
"""
cron_relay.py — VPS-side cron relay with three-tier fallback.

Routing order:
    1. Workstation (LM Studio) — full Hermes tool access
    2. Mobile (Termux/llama.cpp) — lightweight local inference
    3. OpenRouter (cloud API) — ultimate fallback, always available

Usage:
    python3 cron_relay.py --job <job_id>
    python3 cron_relay.py --check  # just report routing decision
"""

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

# ─── Configuration ───────────────────────────────────────────────────────────
# Edit these for your setup
VPS_HEALTH_URL = "http://127.0.0.1:9191/mobile"
MOBILE_PORT = 9192
CRON_JOBS_FILE = os.path.expanduser("~/.hermes/cron/jobs.json")
ROUTING_LOG = os.path.expanduser("~/.hermes/cron_state/mobile_routing_log.json")

# OpenRouter fallback config
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "openrouter/auto"

# Phone hostname pattern for Tailscale discovery
# Matches any peer whose hostname contains these strings (case-insensitive)
MOBILE_HOSTNAME_PATTERNS = ["phone", "mobile", "android", "nubia", "z70"]

# Jobs that MUST run on workstation (require session_search, file access, etc.)
# Add your own job IDs here
WORKSTATION_ONLY_JOBS = set()  # e.g., {"job_id_1", "job_id_2"}


def _load_openrouter_key():
    """Read OpenRouter API key from Hermes config or environment."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key
    for cfg_path in [
        os.path.expanduser("~/.hermes/config.yaml"),
        "/etc/hermes/config.yaml",
    ]:
        try:
            with open(cfg_path) as f:
                content = f.read()
            in_openrouter = False
            for line in content.splitlines():
                if line.strip().startswith("openrouter:") or line.strip().startswith("- openrouter:"):
                    in_openrouter = True
                    continue
                if in_openrouter:
                    if line.strip().startswith("api_key:"):
                        return line.split(":", 1)[1].strip().strip('"').strip("'")
                    if line and not line.startswith(" ") and not line.startswith("\t"):
                        in_openrouter = False
        except Exception:
            pass
    return ""


OPENROUTER_API_KEY = _load_openrouter_key()


def log(msg):
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] {msg}")


def check_workstation():
    """Check if workstation is available."""
    try:
        req = urllib.request.urlopen(VPS_HEALTH_URL, timeout=5)
        data = json.loads(req.read().decode())
        return data.get("workstation_online", False)
    except Exception as e:
        log(f"Health check failed: {e}")
        return False


def get_mobile_ip():
    """Find the mobile node's Tailscale IP by hostname pattern or OS."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            ts_data = json.loads(result.stdout)
            # First pass: match by hostname pattern
            for key, peer in ts_data.get("Peer", {}).items():
                hostname = peer.get("HostName", "").lower()
                for pattern in MOBILE_HOSTNAME_PATTERNS:
                    if pattern in hostname:
                        ips = peer.get("TailscaleIPs", [])
                        if ips:
                            return ips[0]
            # Second pass: match by OS
            for key, peer in ts_data.get("Peer", {}).items():
                if peer.get("OS", "").lower() == "android":
                    ips = peer.get("TailscaleIPs", [])
                    if ips:
                        return ips[0]
    except Exception:
        pass
    return None


def forward_to_mobile(job_id, job_name, job_prompt):
    """Forward a cron job to the mobile node for local execution."""
    mobile_ip = get_mobile_ip()
    if not mobile_ip:
        log("Mobile node not found in Tailscale — cannot forward job")
        return False

    url = f"http://{mobile_ip}:{MOBILE_PORT}/cron/trigger"
    payload = json.dumps({
        "job_name": job_name,
        "job_id": job_id,
        "context": job_prompt[:2000],  # Truncate for mobile context limits
        "source": "vps_relay",
    }).encode()

    log(f"Forwarding job '{job_name}' to mobile at {mobile_ip}")

    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=60)
        result = json.loads(resp.read().decode())
        log_routing(job_id, job_name, "mobile", result.get("ok", False))
        return result.get("ok", False)
    except Exception as e:
        log(f"Failed to reach mobile node: {e}")
        log_routing(job_id, job_name, "mobile", False, str(e))
        return False


def run_via_openrouter(job_id, job_name, job_prompt):
    """Run a cron job via OpenRouter API as ultimate fallback."""
    if not OPENROUTER_API_KEY:
        log("No OpenRouter API key — cannot use tier-3 fallback")
        return False

    log(f"Running job '{job_name}' via OpenRouter")

    payload = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an AI assistant. Execute the requested task concisely "
                    "and accurately. You do not have access to local files, session "
                    "search, or external tools — work only with the information "
                    "provided in the task context."
                ),
            },
            {
                "role": "user",
                "content": f"Cron job: {job_name}\n\nTask:\n{job_prompt}",
            },
        ],
        "max_tokens": 2048,
        "temperature": 0.7,
    }).encode()

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://github.com/YOUR_USERNAME/hermes-mobile-node",
            "X-Title": "Hermes Mobile Fallback",
        },
    )

    try:
        resp = urllib.request.urlopen(req, timeout=120)
        result = json.loads(resp.read().decode())
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")

        log(f"OpenRouter response for '{job_name}': {content[:200]}...")
        log_routing(job_id, job_name, "openrouter", True)
        return True
    except Exception as e:
        log(f"OpenRouter failed for '{job_name}': {e}")
        log_routing(job_id, job_name, "openrouter", False, str(e))
        return False


def log_routing(job_id, job_name, target, success, error=None):
    """Log routing decisions for debugging."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "job_name": job_name,
        "routed_to": target,
        "success": success,
    }
    if error:
        entry["error"] = error

    log_data = []
    try:
        with open(ROUTING_LOG) as f:
            log_data = json.load(f)
    except Exception:
        pass

    log_data.append(entry)
    log_data = log_data[-100:]  # Keep last 100 entries

    os.makedirs(os.path.dirname(ROUTING_LOG), exist_ok=True)
    with open(ROUTING_LOG, "w") as f:
        json.dump(log_data, f, indent=2)


def load_jobs():
    try:
        with open(CRON_JOBS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"jobs": []}


def main():
    if "--check" in sys.argv:
        ws = check_workstation()
        mobile_ip = get_mobile_ip()
        router_key = bool(OPENROUTER_API_KEY)

        if ws:
            routing = "tier-1: workstation (local)"
        elif mobile_ip:
            routing = f"tier-2: mobile ({mobile_ip})"
        elif router_key:
            routing = "tier-3: OpenRouter (cloud fallback)"
        else:
            routing = "all tiers unreachable"

        print(json.dumps({
            "workstation_online": ws,
            "mobile_ip": mobile_ip,
            "mobile_available": bool(mobile_ip),
            "openrouter_configured": router_key,
            "routing": routing,
        }, indent=2))
        return

    if "--job" in sys.argv:
        job_id = sys.argv[sys.argv.index("--job") + 1] if "--job" in sys.argv else None
        if not job_id:
            print("Usage: cron_relay.py --job <job_id>")
            sys.exit(1)

        jobs_data = load_jobs()
        job = None
        for j in jobs_data.get("jobs", []):
            if j.get("id") == job_id:
                job = j
                break

        if not job:
            log(f"Job {job_id} not found")
            sys.exit(1)

        job_name = job.get("name", job_id)
        job_prompt = job.get("prompt", "")

        ws_online = check_workstation()
        is_workstation_only = job_id in WORKSTATION_ONLY_JOBS

        if ws_online and not is_workstation_only:
            log(f"Workstation online — job '{job_name}' runs normally on VPS")
            log_routing(job_id, job_name, "vps", True)
            return

        if is_workstation_only and not ws_online:
            log(f"Job '{job_name}' requires workstation — trying mobile then OpenRouter")

        # Tier 2: Try mobile
        mobile_ip = get_mobile_ip()
        if mobile_ip:
            log(f"Workstation offline, mobile at {mobile_ip} — forwarding '{job_name}'")
            success = forward_to_mobile(job_id, job_name, job_prompt)
            if success:
                return
            log(f"Mobile failed for '{job_name}' — trying next tier")
        else:
            log(f"Workstation offline, no mobile — trying OpenRouter for '{job_name}'")

        # Tier 3: OpenRouter
        if OPENROUTER_API_KEY:
            log(f"Falling back to OpenRouter for '{job_name}'")
            success = run_via_openrouter(job_id, job_name, job_prompt)
            if success:
                return
            log(f"OpenRouter also failed — job will be retried next cycle")
        else:
            log(f"No OpenRouter key — job '{job_name}' skipped (all tiers unreachable)")
            log_routing(job_id, job_name, "skipped", False, "all_tiers_unreachable")

        sys.exit(1)


if __name__ == "__main__":
    main()
