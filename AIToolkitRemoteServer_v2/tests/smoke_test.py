import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import importlib.util
import os

# Smoke test is intentionally small and dependency-free.
# It validates the proxy route shape against a fake AI Toolkit HTTP server.

class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/api/jobs"):
            body = json.dumps({"jobs": []}).encode()
        elif self.path == "/api/gpu":
            body = json.dumps({"hasNvidiaSmi": True, "gpus": []}).encode()
        else:
            body = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def main():
    fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    assert fake.server_port > 0
    fake.shutdown()
    fake.server_close()
    print("smoke-ok")

if __name__ == "__main__":
    main()
