#!/usr/bin/env python3
"""
workstation_health.py — Health endpoint for the mobile AI continuity node.

Runs on the VPS. Polls the workstation (LM Studio) and reports availability.
Two endpoints:
  /health — full diagnostics
  /mobile — minimal decision for phone (run_local: true/false)

Usage:
    python3 workstation_health.py            # single-shot check (prints JSON)
    python3 workstation_health.py --serve    # start HTTP server
    python3 workstation_health.py --check    # same as no args
"""

import json
import os
import subprocess
import sys
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

# ─── Configuration ───────────────────────────────────────────────────────────
# Edit these for your setup
WORKSTATION_HOST = "YOUR_WORKSTATION_TAILSCALE_IP"  # e.g., 100.x.x.x
LMSTUDIO_PORT = 1235
HEALTH_PORT = 9191


def check_tailscale():
    """Check workstation availability via Tailscale."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            ts_data = json.loads(result.stdout)
            for key, peer in ts_data.get("Peer", {}).items():
                hostname = peer.get("HostName", "")
                if WORKSTATION_HOST in peer.get("TailscaleIPs", []):
                    return {
                        "ok": peer.get("Online", False),
                        "hostname": hostname,
                        "online": peer.get("Online", False),
                        "last_seen": peer.get("LastSeen", "unknown"),
                    }
        return {"ok": False, "error": "workstation not found in Tailscale peers"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_lmstudio():
    """Check if LM Studio is responding on the workstation."""
    url = f"http://{WORKSTATION_HOST}:{LMSTUDIO_PORT}/v1/models"
    try:
        req = urllib.request.urlopen(url, timeout=5)
        data = json.loads(req.read().decode())
        models = [m.get("id", "?") for m in data.get("data", [])]
        return {"ok": True, "models_loaded": len(models), "models": models[:5]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_vps_stats():
    """Get VPS disk/memory/load stats."""
    stats = {}
    try:
        df = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        lines = df.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            stats["disk_usage_pct"] = parts[4]
            stats["disk_avail"] = parts[3]
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    mem[k.strip()] = v.strip().split()[0]
            total = int(mem.get("MemTotal", 0))
            avail = int(mem.get("MemAvailable", 0))
            if total:
                stats["memory_total_mb"] = total // 1024
                stats["memory_used_mb"] = (total - avail) // 1024
                stats["memory_pct"] = round((total - avail) / total * 100, 1)
    except Exception:
        pass
    try:
        with open("/proc/loadavg") as f:
            stats["load_1m"] = float(f.read().split()[0])
    except Exception:
        pass
    return stats


def build_health_response():
    """Build the full health response."""
    ts = check_tailscale()
    ts_ok = ts.get("ok", False)

    lm = {"ok": False, "error": "workstation offline"}
    if ts_ok:
        lm = check_lmstudio()

    vps = get_vps_stats()

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "workstation": {
            "host": WORKSTATION_HOST,
            "available": ts_ok and lm.get("ok", False),
            "tailscale": ts,
            "lmstudio": lm,
        },
        "vps": vps,
        "mobile_should_run_local": not (ts_ok and lm.get("ok", False)),
    }


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = self.path.split("?")[0]
        if parsed == "/health":
            data = build_health_response()
            self._respond(200, data)
        elif parsed == "/mobile":
            full = build_health_response()
            ws = full.get("workstation", {})
            self._respond(200, {
                "run_local": full.get("mobile_should_run_local", True),
                "workstation_online": ws.get("available", False),
                "timestamp": full.get("timestamp", ""),
            })
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
    if "--serve" in sys.argv:
        server = HTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
        print(f"Health endpoint listening on port {HEALTH_PORT}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
    else:
        print(json.dumps(build_health_response(), indent=2))


if __name__ == "__main__":
    main()
