#!/usr/bin/env python3
"""
hermes_mobile.py — Lightweight service layer for the mobile AI continuity node.

Runs on Android (Termux). Acts as a local inference endpoint when the
workstation is offline. Routes through VPS Hermes when connectivity allows.

Services:
  - /health          — mobile node health
  - /workstation     — check if workstation is available
  - /infer           — run local inference (delegates to llama-server)
  - /cron/trigger    — trigger a named cron job locally
  - /status          — full node status
  - /model/load      — load a model into RAM
  - /model/unload    — unload a model from RAM

The VPS Hermes cron system POSTs to /cron/trigger when it detects
the workstation is offline and the job is mobile-compatible.
"""

import json
import os
import subprocess
import signal
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

# ─── Configuration ───────────────────────────────────────────────────────────
# Edit these for your setup
NODE_NAME = "mobile-node"
VPS_HOST = "YOUR_VPS_TAILSCALE_IP"      # e.g., 100.x.x.x
VPS_HEALTH_PORT = 9191
MOBILE_PORT = 9192
MODEL_DIR = "/data/data/com.termux/files/home/storage/shared/AI_Models"
STATE_FILE = "/data/data/com.termux/files/home/.hermes_mobile_state"
LOG_FILE = "/data/data/com.termux/files/home/.hermes_mobile.log"

# Model server ports (managed by model_manager.sh)
MODEL_PORTS = {
    "nemotron": 8081,
    "qwen": 8082,
}

# Which model to use for which job type
JOB_MODEL_MAP = {
    "cron": "nemotron",
    "default": "nemotron",
}

LOG_CONTEXT_BYTES = 2000  # max chars of context to pass to model


def log(msg):
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"jobs_run": [], "started_at": datetime.now(timezone.utc).isoformat()}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def check_workstation():
    """Check VPS health endpoint to see if workstation is online."""
    import urllib.request
    url = f"http://{VPS_HOST}:{VPS_HEALTH_PORT}/mobile"
    try:
        req = urllib.request.urlopen(url, timeout=5)
        data = json.loads(req.read().decode())
        return {
            "online": data.get("workstation_online", False),
            "run_local": data.get("run_local", True),
        }
    except Exception as e:
        return {"online": False, "run_local": True, "error": str(e)}


def run_local_inference(prompt, model="nemotron", max_tokens=1024):
    """Send inference request to local llama-server."""
    import urllib.request

    port = MODEL_PORTS.get(model, MODEL_PORTS["nemotron"])
    url = f"http://127.0.0.1:{port}/v1/chat/completions"

    # Check if server is running
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
    except Exception:
        # Try to start it
        log(f"llama-server for {model} not running, attempting to start...")
        subprocess.run(
            ["bash", os.path.expanduser("~/.hermes/scripts/model_manager.sh"), "load", model],
            timeout=60,
        )
        time.sleep(3)

    payload = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        result = json.loads(resp.read().decode())
        text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {"ok": True, "text": text, "model": model}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_cron_job(job_name, job_context=""):
    """Execute a cron job locally using the appropriate model."""
    model = JOB_MODEL_MAP.get("cron")
    if not model:
        model = JOB_MODEL_MAP["default"]

    log(f"Running cron job '{job_name}' on {model}")

    full_prompt = (
        f"You are an AI assistant. Execute this cron job:\n\n"
        f"Job: {job_name}\n\n"
        f"Context:\n{job_context}\n\n"
        f"Execute the job and provide a concise result."
    )

    result = run_local_inference(full_prompt, model=model, max_tokens=2048)

    # Record in state
    state = load_state()
    state.setdefault("jobs_run", []).append({
        "name": job_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "ok": result.get("ok", False),
    })
    save_state(state)

    return result


def get_node_status():
    """Full status of the mobile node."""
    ws = check_workstation()
    state = load_state()

    # Check which models are running
    models_running = {}
    for name, port in MODEL_PORTS.items():
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
            models_running[name] = True
        except Exception:
            models_running[name] = False

    return {
        "node": NODE_NAME,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "workstation": ws,
        "models_running": models_running,
        "total_jobs_run": len(state.get("jobs_run", [])),
        "recent_jobs": state.get("jobs_run", [])[-5:],
    }


class MobileHandler(BaseHTTPRequestHandler):
    """HTTP handler for the mobile service layer."""

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self._respond(200, {"ok": True, "node": NODE_NAME})

        elif parsed.path == "/workstation":
            self._respond(200, check_workstation())

        elif parsed.path == "/status":
            self._respond(200, get_node_status())

        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        parsed = urlparse(self.path)

        if parsed.path == "/infer":
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            prompt = data.get("prompt", "")
            model = data.get("model", "nemotron")
            max_tokens = data.get("max_tokens", 1024)

            if not prompt:
                self._respond(400, {"error": "missing prompt"})
            else:
                result = run_local_inference(prompt, model, max_tokens)
                self._respond(200, result)

        elif parsed.path == "/cron/trigger":
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            job_name = data.get("job_name", "unknown")
            job_context = data.get("context", "")

            log(f"Cron trigger received: {job_name}")
            result = run_cron_job(job_name, job_context)

            self._respond(200, {
                "ok": result.get("ok", False),
                "job": job_name,
                "model": result.get("model"),
                "result": result.get("text", "")[:500] if result.get("ok") else result.get("error"),
            })

        elif parsed.path == "/model/load":
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            model = data.get("model", "nemotron")

            if model not in MODEL_PORTS:
                self._respond(400, {"error": f"unknown model '{model}'"})
            else:
                log(f"Loading model: {model}")
                ret = subprocess.run(
                    ["bash", os.path.expanduser("~/.hermes/scripts/model_manager.sh"), "load", model],
                    capture_output=True, text=True, timeout=90,
                )
                self._respond(200 if ret.returncode == 0 else 500, {
                    "ok": ret.returncode == 0,
                    "model": model,
                    "output": ret.stdout + ret.stderr,
                })

        elif parsed.path == "/model/unload":
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            model = data.get("model", "all")

            if model == "all":
                results = {}
                for m in MODEL_PORTS:
                    ret = subprocess.run(
                        ["bash", os.path.expanduser("~/.hermes/scripts/model_manager.sh"), "unload", m],
                        capture_output=True, text=True, timeout=30,
                    )
                    results[m] = ret.returncode == 0
                self._respond(200, {"ok": True, "results": results})
            else:
                ret = subprocess.run(
                    ["bash", os.path.expanduser("~/.hermes/scripts/model_manager.sh"), "unload", model],
                    capture_output=True, text=True, timeout=30,
                )
                self._respond(200 if ret.returncode == 0 else 500, {
                    "ok": ret.returncode == 0,
                    "model": model,
                })

        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code, data):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress default logging


def run_daemon():
    """Run as a background daemon that monitors health and manages models."""
    log(f"Hermes Mobile daemon starting on port {MOBILE_PORT}")

    # Start HTTP server in a thread
    from threading import Thread
    server = HTTPServer(("0.0.0.0", MOBILE_PORT), MobileHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    log(f"HTTP server listening on port {MOBILE_PORT}")

    # Main loop — periodic health checks and model management
    while True:
        try:
            ws = check_workstation()
            if ws.get("online"):
                # Workstation is back — unload local models to free RAM
                log("Workstation online — unloading local models")
                subprocess.run(
                    ["bash", os.path.expanduser("~/.hermes/scripts/model_manager.sh"), "unload", "all"],
                    timeout=15,
                )
            else:
                # Workstation offline — ensure primary model is loaded
                log("Workstation offline — ensuring primary model loaded")
                subprocess.run(
                    ["bash", os.path.expanduser("~/.hermes/scripts/model_manager.sh"), "primary"],
                    timeout=90,
                )
        except Exception as e:
            log(f"Daemon loop error: {e}")

        time.sleep(60)  # check every 60s


def main():
    if "--daemon" in sys.argv:
        run_daemon()
    elif "--check" in sys.argv:
        print(json.dumps(get_node_status(), indent=2))
    else:
        # Single-shot HTTP server (foreground)
        server = HTTPServer(("0.0.0.0", MOBILE_PORT), MobileHandler)
        log(f"Hermes Mobile server on port {MOBILE_PORT}")
        print(f"Hermes Mobile listening on http://0.0.0.0:{MOBILE_PORT}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()


if __name__ == "__main__":
    main()
