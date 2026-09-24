"""Portfolio MRI policy/goals API.

Runs on 127.0.0.1:8302, proxied by nginx at /mri-api/ (same basic auth as the
site). Endpoints:
  GET  /policy    -> current /root/portfolio-mri/policy.json (or template)
  POST /policy    -> validate + save policy.json (JSON body)
  POST /refresh   -> run gen_mri_data.py, return its stdout/stderr

No auth here on purpose: nginx enforces basic auth before proxying, and the
upstream binds to loopback only.
"""
import json
import pathlib
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

POLICY_PATH = pathlib.Path("/root/portfolio-mri/policy.json")
GEN = pathlib.Path("/root/portfolio-mri/gen_mri_data.py")
LOCK = threading.Lock()
MAX_BODY = 512 * 1024


def load_policy():
    if POLICY_PATH.exists():
        return POLICY_PATH.read_text(), "application/json"
    example = pathlib.Path("/root/portfolio-mri/policy.example.json")
    if example.exists():
        return example.read_text(), "application/json"
    return "{}", "application/json"


def validate_policy(raw: bytes):
    """Minimal schema sanity: valid JSON object with known top-level keys."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")
    if not isinstance(doc, dict):
        raise ValueError("Policy must be a JSON object")
    known = {"contributions", "holding", "rebalancing", "risk", "goals", "_comment"}
    bad = set(doc) - known
    if bad:
        raise ValueError(f"Unknown top-level keys: {', '.join(sorted(bad))}")
    goals = doc.get("goals")
    if goals is not None:
        if not isinstance(goals, list):
            raise ValueError("goals must be a list")
        for i, g in enumerate(goals):
            if not isinstance(g, dict):
                raise ValueError(f"goals[{i}] must be an object")
            if not (g.get("name") or "").strip():
                raise ValueError(f"goals[{i}].name is required")
            if not isinstance(g.get("target_inr"), (int, float)) or g["target_inr"] <= 0:
                raise ValueError(f"goals[{i}].target_inr must be a positive number")
    return doc


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") == "/policy":
            text, ctype = load_policy()
            self._send(200, text.encode(), ctype)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(400, {"error": "bad body size"})
            return
        raw = self.rfile.read(length) if length else b""
        if self.path.rstrip("/") == "/policy":
            try:
                doc = validate_policy(raw)
            except ValueError as e:
                self._send(400, {"error": str(e)})
                return
            with LOCK:
                tmp = POLICY_PATH.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(doc, indent=2) + "\n")
                tmp.replace(POLICY_PATH)
            self._send(200, {"ok": True, "path": str(POLICY_PATH)})
        elif self.path.rstrip("/") == "/refresh":
            try:
                proc = subprocess.run(
                    ["python3", str(GEN)], capture_output=True, text=True, timeout=120
                )
            except subprocess.TimeoutExpired:
                self._send(504, {"error": "generator timed out"})
                return
            self._send(200 if proc.returncode == 0 else 500, {
                "ok": proc.returncode == 0,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            })
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8302), Handler).serve_forever()
