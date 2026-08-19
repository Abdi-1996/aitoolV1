from __future__ import annotations

import base64
import hmac
import http.client
import json
import mimetypes
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, Tuple

APP_NAME = "AIToolkitRemoteServer"
VERSION = "2.0.0"
PREFIX = "/api/aitk"


def runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = runtime_dir() / "AIToolkitRemoteServer.json"


def random_key() -> str:
    return secrets.token_urlsafe(24)


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8111
    access_key: str = ""
    ai_toolkit_url: str = "http://127.0.0.1:8675"
    start_command: str = ""
    start_cwd: str = ""
    datasets_dir: str = ""
    request_timeout: float = 15.0
    max_upload_mb: int = 4096
    autostart_windows: bool = False

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            cfg = cls(access_key=random_key())
            cfg.save()
            return cfg
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = cls(
                host=str(raw.get("host", "0.0.0.0")),
                port=int(raw.get("port", 8111)),
                access_key=str(raw.get("access_key", "")) or random_key(),
                ai_toolkit_url=str(raw.get("ai_toolkit_url", "http://127.0.0.1:8675")).rstrip("/"),
                start_command=str(raw.get("start_command", "")),
                start_cwd=str(raw.get("start_cwd", "")),
                datasets_dir=str(raw.get("datasets_dir", "")),
                request_timeout=float(raw.get("request_timeout", 15.0)),
                max_upload_mb=int(raw.get("max_upload_mb", 4096)),
                autostart_windows=bool(raw.get("autostart_windows", False)),
            )
            # Never turn this app into a generic remote HTTP proxy.
            parsed = urllib.parse.urlsplit(cfg.ai_toolkit_url)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                cfg.ai_toolkit_url = "http://127.0.0.1:8675"
            if not cfg.access_key:
                cfg.access_key = random_key()
            cfg.save()
            return cfg
        except Exception:
            cfg = cls(access_key=random_key())
            cfg.save()
            return cfg

    def save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")


CONFIG_LOCK = threading.RLock()
CONFIG = Config.load()
SERVER: Optional[ThreadingHTTPServer] = None
SERVER_THREAD: Optional[threading.Thread] = None
LAST_LAUNCH = 0.0
LAUNCH_LOCK = threading.Lock()


def cfg() -> Config:
    with CONFIG_LOCK:
        return CONFIG


def update_config(new_cfg: Config) -> None:
    global CONFIG
    with CONFIG_LOCK:
        CONFIG = new_cfg
        CONFIG.save()
    apply_windows_autostart(new_cfg.autostart_windows)


def json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def write_response(handler: BaseHTTPRequestHandler, status: int, data: bytes,
                   content_type: str = "application/json; charset=utf-8",
                   headers: Optional[Dict[str, str]] = None) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-AIToolkit-Remote-Server", VERSION)
    if headers:
        for k, v in headers.items():
            lk = k.lower()
            if lk in {"content-length", "content-type", "transfer-encoding", "connection"}:
                continue
            handler.send_header(k, v)
    handler.end_headers()
    if handler.command != "HEAD":
        try:
            handler.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def write_json(handler: BaseHTTPRequestHandler, status: int, obj) -> None:
    write_response(handler, status, json_bytes(obj))


def read_body(handler: BaseHTTPRequestHandler, max_bytes: Optional[int] = None) -> bytes:
    try:
        length = int(handler.headers.get("Content-Length", "0") or "0")
    except ValueError:
        raise ValueError("invalid Content-Length")
    limit = max_bytes if max_bytes is not None else cfg().max_upload_mb * 1024 * 1024
    if length < 0 or length > limit:
        raise ValueError("request too large")
    return handler.rfile.read(length) if length else b""


def upstream_parts() -> Tuple[str, str, int, str]:
    c = cfg()
    u = urllib.parse.urlsplit(c.ai_toolkit_url)
    scheme = u.scheme or "http"
    host = u.hostname or "127.0.0.1"
    port = u.port or (443 if scheme == "https" else 80)
    base_path = u.path.rstrip("/")
    return scheme, host, port, base_path


def upstream_small(method: str, path: str, body: bytes = b"",
                   headers: Optional[Dict[str, str]] = None,
                   timeout: Optional[float] = None) -> Tuple[int, bytes, Dict[str, str]]:
    c = cfg()
    url = c.ai_toolkit_url + path
    req_headers = {"Accept": "application/json, image/*, video/*, audio/*, application/octet-stream"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(
        url,
        data=(body if method in {"POST", "PUT", "PATCH"} else None),
        method=method,
        headers=req_headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or c.request_timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers.items())


def proxy_small(handler: BaseHTTPRequestHandler, method: str, path: str,
                body: bytes = b"", content_type: Optional[str] = None) -> None:
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        status, data, resp_headers = upstream_small(method, path, body, headers)
        ctype = resp_headers.get("Content-Type", "application/json; charset=utf-8")
        extra = {k: v for k, v in resp_headers.items()
                 if k.lower() in {"content-disposition", "etag", "last-modified", "accept-ranges", "content-range"}}
        write_response(handler, status, data, ctype, extra)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        write_json(handler, 503, {"ok": False, "error": "AI Toolkit is offline", "detail": str(e)})


def proxy_upload_stream(handler: BaseHTTPRequestHandler, upstream_path: str) -> None:
    """Forward a large multipart upload to AI Toolkit without buffering the whole file on the PC."""
    c = cfg()
    try:
        total = int(handler.headers.get("Content-Length", "0") or "0")
    except ValueError:
        write_json(handler, 400, {"error": "invalid Content-Length"})
        return
    if total <= 0:
        write_json(handler, 400, {"error": "empty upload"})
        return
    if total > c.max_upload_mb * 1024 * 1024:
        write_json(handler, 413, {"error": "request too large"})
        return

    scheme, host, port, base_path = upstream_parts()
    conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(host, port, timeout=max(c.request_timeout, 120))
    try:
        target = base_path + upstream_path
        conn.putrequest("POST", target, skip_accept_encoding=True)
        conn.putheader("Content-Type", handler.headers.get("Content-Type", "application/octet-stream"))
        conn.putheader("Content-Length", str(total))
        conn.putheader("Accept", "application/json")
        conn.endheaders()
        remaining = total
        while remaining:
            chunk = handler.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ConnectionError("client upload ended early")
            conn.send(chunk)
            remaining -= len(chunk)
        resp = conn.getresponse()
        data = resp.read()
        write_response(handler, resp.status, data, resp.getheader("Content-Type") or "application/json; charset=utf-8")
    except (OSError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
        write_json(handler, 503, {"ok": False, "error": "upload proxy failed", "detail": str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


def proxy_download_stream(handler: BaseHTTPRequestHandler, upstream_path: str) -> None:
    c = cfg()
    scheme, host, port, base_path = upstream_parts()
    conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(host, port, timeout=max(c.request_timeout, 120))
    try:
        headers = {"Accept": "*/*"}
        for key in ("Range", "If-None-Match", "If-Modified-Since"):
            if handler.headers.get(key):
                headers[key] = handler.headers[key]
        conn.request("GET", base_path + upstream_path, headers=headers)
        resp = conn.getresponse()

        handler.send_response(resp.status)
        handler.send_header("Content-Type", resp.getheader("Content-Type") or "application/octet-stream")
        handler.send_header("X-AIToolkit-Remote-Server", VERSION)
        for key in ("Content-Length", "Content-Disposition", "ETag", "Last-Modified",
                    "Accept-Ranges", "Content-Range", "Cache-Control"):
            val = resp.getheader(key)
            if val is not None:
                handler.send_header(key, val)
        handler.end_headers()
        if handler.command == "HEAD":
            return
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        return
    except (OSError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
        try:
            write_json(handler, 503, {"ok": False, "error": "download proxy failed", "detail": str(e)})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def is_ai_toolkit_online() -> bool:
    try:
        status, _, _ = upstream_small("GET", "/api/jobs?only_active=true", timeout=2.5)
        return 200 <= status < 500
    except Exception:
        return False


def launch_ai_toolkit() -> Tuple[bool, str]:
    global LAST_LAUNCH
    c = cfg()
    if is_ai_toolkit_online():
        return True, "already_running"
    if not c.start_command.strip():
        return False, "start_command_not_configured"
    with LAUNCH_LOCK:
        now = time.time()
        if now - LAST_LAUNCH < 5:
            return True, "launch_already_requested"
        LAST_LAUNCH = now
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            subprocess.Popen(
                c.start_command,
                cwd=(c.start_cwd.strip() or None),
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            return True, "launch_requested"
        except Exception as e:
            return False, str(e)


def safe_job_id(value: str) -> Optional[str]:
    value = urllib.parse.unquote(value).strip()
    if not value or len(value) > 160:
        return None
    if any(ch in value for ch in "\\/\r\n\t"):
        return None
    return value


def normalized_dataset_name(name: str) -> Optional[str]:
    name = name.strip()
    if not name or len(name) > 120:
        return None
    if name in {".", ".."} or any(ch in name for ch in "/\\:\x00\r\n\t"):
        return None
    return name


def resolved_datasets_dir() -> Optional[Path]:
    c = cfg()
    candidates = []
    if c.datasets_dir.strip():
        candidates.append(Path(c.datasets_dir.strip()))
    if c.start_cwd.strip():
        base = Path(c.start_cwd.strip())
        candidates += [base / "datasets", base.parent / "datasets"]
    for p in candidates:
        try:
            if p.exists() and p.is_dir():
                return p.resolve()
        except Exception:
            continue
    # If the user explicitly configured a datasets_dir that doesn't exist yet, allow creation.
    if c.datasets_dir.strip():
        try:
            p = Path(c.datasets_dir.strip()).resolve()
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            return None
    return None


def create_dataset_local(name: str) -> Tuple[bool, str]:
    safe = normalized_dataset_name(name)
    if not safe:
        return False, "invalid_dataset_name"
    root = resolved_datasets_dir()
    if root is None:
        return False, "datasets_dir_not_configured"
    try:
        target = (root / safe).resolve()
        if root != target and root not in target.parents:
            return False, "invalid_dataset_path"
        target.mkdir(parents=True, exist_ok=True)
        return True, safe
    except Exception as e:
        return False, str(e)


def auth_ok(handler: BaseHTTPRequestHandler) -> bool:
    expected = cfg().access_key
    if not expected:
        return True
    xkey = handler.headers.get("X-PCRemote-Key", "")
    auth = handler.headers.get("Authorization", "")
    bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
    supplied = xkey or bearer
    return bool(supplied) and hmac.compare_digest(supplied, expected)


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Keep a lightweight log file next to the executable.
        try:
            line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.client_address[0]} {fmt % args}\n"
            with (runtime_dir() / "AIToolkitRemoteServer.log").open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _route(self):
        split = urllib.parse.urlsplit(self.path)

        if split.path == "/":
            data = (
                f"AI Toolkit Remote Server {VERSION}\n"
                f"AI Toolkit: {'ONLINE' if is_ai_toolkit_online() else 'OFFLINE'}\n"
                f"API: {PREFIX}/status\n"
            ).encode("utf-8")
            write_response(self, 200, data, "text/plain; charset=utf-8")
            return

        if not (split.path == PREFIX or split.path.startswith(PREFIX + "/")):
            write_json(self, 404, {"error": "not found"})
            return

        if not auth_ok(self):
            write_json(self, 401, {"ok": False, "error": "unauthorized"})
            return

        rel = split.path[len(PREFIX):] or "/"
        query = urllib.parse.parse_qs(split.query, keep_blank_values=True)
        method = self.command.upper()

        try:
            if method == "GET" and rel in {"/", "/status"}:
                c = cfg()
                write_json(self, 200, {
                    "ok": True,
                    "online": is_ai_toolkit_online(),
                    "upstream": c.ai_toolkit_url,
                    "can_launch": bool(c.start_command.strip()),
                    "bridge_version": VERSION,
                    "standalone": True,
                    "features": [
                        "jobs", "gpu", "logs", "loss", "samples", "files",
                        "datasets", "upload", "launch", "streaming_downloads"
                    ],
                })
                return

            if method == "POST" and rel == "/launch":
                ok, state = launch_ai_toolkit()
                write_json(self, 200 if ok else 409, {"ok": ok, "state": state})
                return

            if method == "GET" and rel == "/gpu":
                proxy_small(self, "GET", "/api/gpu")
                return

            if rel == "/jobs" and method == "GET":
                suffix = ("?" + split.query) if split.query else ""
                proxy_small(self, "GET", "/api/jobs" + suffix)
                return

            if rel == "/jobs" and method == "POST":
                body = read_body(self, 16 * 1024 * 1024)
                proxy_small(self, "POST", "/api/jobs", body,
                            self.headers.get("Content-Type", "application/json"))
                return

            if method == "GET" and rel == "/datasets":
                proxy_small(self, "GET", "/api/datasets/list")
                return

            if method == "POST" and rel == "/datasets/create":
                body = read_body(self, 1024 * 1024)
                try:
                    name = json.loads(body.decode("utf-8")).get("name", "")
                except Exception:
                    write_json(self, 400, {"success": False, "error": "invalid_json"})
                    return
                ok, result = create_dataset_local(str(name))
                if ok:
                    write_json(self, 200, {"success": True, "name": result})
                else:
                    write_json(self, 409, {"success": False, "error": result})
                return

            if method == "POST" and rel == "/datasets/images":
                body = read_body(self, 1024 * 1024)
                proxy_small(self, "POST", "/api/datasets/listImages", body,
                            self.headers.get("Content-Type", "application/json"))
                return

            if method == "POST" and rel == "/datasets/upload":
                proxy_upload_stream(self, "/api/datasets/upload")
                return

            if method in {"GET", "HEAD"} and rel in {"/media", "/file"}:
                raw_path = (query.get("path") or [""])[0]
                if not raw_path or len(raw_path) > 4096 or "\x00" in raw_path:
                    write_json(self, 400, {"error": "missing path"})
                    return
                encoded = urllib.parse.quote(raw_path, safe="")
                upstream = ("/api/img/" if rel == "/media" else "/api/files/") + encoded
                if rel == "/media" and (query.get("thumb") or [""])[0] in {"1", "true"}:
                    upstream += "?thumb=1"
                proxy_download_stream(self, upstream)
                return

            parts = [p for p in rel.split("/") if p]
            if len(parts) >= 2 and parts[0] == "jobs":
                job_id = safe_job_id(parts[1])
                if not job_id:
                    write_json(self, 400, {"error": "invalid job id"})
                    return
                qid = urllib.parse.quote(job_id, safe="")

                if len(parts) == 2 and method == "GET":
                    proxy_small(self, "GET", "/api/jobs?id=" + qid)
                    return

                if len(parts) == 3:
                    action = parts[2]
                    if action in {"start", "stop", "save"} and method == "POST":
                        upstream_action = "save_now" if action == "save" else action
                        proxy_small(self, "GET", f"/api/jobs/{qid}/{upstream_action}")
                        return
                    if action == "delete" and method in {"POST", "DELETE"}:
                        proxy_small(self, "GET", f"/api/jobs/{qid}/delete")
                        return
                    if action == "log" and method == "GET":
                        offset = (query.get("offset") or ["0"])[0]
                        if not str(offset).isdigit():
                            offset = "0"
                        proxy_small(self, "GET", f"/api/jobs/{qid}/log?offset={offset}")
                        return
                    if action == "loss" and method == "GET":
                        allowed = []
                        for key in ("key", "limit", "since_step", "stride"):
                            if key in query:
                                allowed.append(urllib.parse.urlencode({key: query[key][0]}))
                        suffix = ("?" + "&".join(allowed)) if allowed else ""
                        proxy_small(self, "GET", f"/api/jobs/{qid}/loss{suffix}")
                        return
                    if action == "samples" and method == "GET":
                        proxy_small(self, "GET", f"/api/jobs/{qid}/samples")
                        return
                    if action == "files" and method == "GET":
                        proxy_small(self, "GET", f"/api/jobs/{qid}/files")
                        return

            write_json(self, 404, {"error": "unknown AI Toolkit route", "path": rel})
        except ValueError as e:
            code = 413 if "large" in str(e) else 400
            write_json(self, code, {"error": str(e)})
        except Exception as e:
            write_json(self, 500, {"error": "server_error", "detail": str(e)})

    def do_GET(self): self._route()
    def do_HEAD(self): self._route()
    def do_POST(self): self._route()
    def do_DELETE(self): self._route()


def start_http_server() -> Tuple[bool, str]:
    global SERVER, SERVER_THREAD
    if SERVER is not None:
        return True, "already_running"
    c = cfg()
    try:
        SERVER = ThreadingHTTPServer((c.host, c.port), Handler)
        SERVER.daemon_threads = True
        SERVER_THREAD = threading.Thread(target=SERVER.serve_forever, name="AITKRemoteHTTP", daemon=True)
        SERVER_THREAD.start()
        return True, f"{c.host}:{c.port}"
    except Exception as e:
        SERVER = None
        return False, str(e)


def stop_http_server() -> None:
    global SERVER, SERVER_THREAD
    s = SERVER
    SERVER = None
    SERVER_THREAD = None
    if s is not None:
        try:
            s.shutdown()
            s.server_close()
        except Exception:
            pass


def restart_http_server() -> Tuple[bool, str]:
    stop_http_server()
    return start_http_server()


def local_ipv4_addresses():
    found = []
    try:
        for _, _, _, _, sockaddr in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = sockaddr[0]
            if not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except Exception:
        pass
    # Tailscale CLI, if installed.
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        out = subprocess.check_output(["tailscale", "ip", "-4"], text=True, timeout=2, creationflags=flags).strip()
        for line in out.splitlines():
            ip = line.strip()
            if ip and ip not in found:
                found.insert(0, ip)
    except Exception:
        pass
    return found or ["127.0.0.1"]


def startup_cmd_path() -> Optional[Path]:
    if os.name != "nt":
        return None
    appdata = os.getenv("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{APP_NAME}.cmd"


def apply_windows_autostart(enabled: bool) -> None:
    p = startup_cmd_path()
    if p is None:
        return
    try:
        if enabled:
            exe = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
            p.write_text(f'@echo off\nstart "" "{exe}"\n', encoding="utf-8")
        else:
            if p.exists():
                p.unlink()
    except Exception:
        pass


def run_gui():
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox, filedialog
    except Exception:
        # Headless fallback.
        ok, info = start_http_server()
        if not ok:
            raise RuntimeError(info)
        while True:
            time.sleep(3600)

    root = tk.Tk()
    root.title(f"AI Toolkit Remote Server {VERSION}")
    root.geometry("760x650")
    root.minsize(700, 600)

    outer = ttk.Frame(root, padding=16)
    outer.pack(fill="both", expand=True)

    title = ttk.Label(outer, text="AI Toolkit Remote Server", font=("Segoe UI", 18, "bold"))
    title.pack(anchor="w")
    subtitle = ttk.Label(outer, text="Отдельный сервер для управления AI Toolkit с iPhone")
    subtitle.pack(anchor="w", pady=(0, 14))

    status_frame = ttk.LabelFrame(outer, text="Статус", padding=12)
    status_frame.pack(fill="x")
    server_var = tk.StringVar(value="Сервер: запуск...")
    toolkit_var = tk.StringVar(value="AI Toolkit: проверка...")
    ttk.Label(status_frame, textvariable=server_var, font=("Segoe UI", 10, "bold")).pack(anchor="w")
    ttk.Label(status_frame, textvariable=toolkit_var).pack(anchor="w", pady=(4, 0))

    conn_frame = ttk.LabelFrame(outer, text="Подключение iPhone", padding=12)
    conn_frame.pack(fill="x", pady=(12, 0))
    address_var = tk.StringVar()
    key_var = tk.StringVar()
    ttk.Label(conn_frame, text="Адрес:").grid(row=0, column=0, sticky="w")
    ttk.Entry(conn_frame, textvariable=address_var, state="readonly").grid(row=0, column=1, sticky="ew", padx=8)
    ttk.Button(conn_frame, text="Копировать", command=lambda: copy_text(address_var.get())).grid(row=0, column=2)
    ttk.Label(conn_frame, text="Ключ:").grid(row=1, column=0, sticky="w", pady=(8, 0))
    ttk.Entry(conn_frame, textvariable=key_var, state="readonly").grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 0))
    ttk.Button(conn_frame, text="Копировать", command=lambda: copy_text(key_var.get())).grid(row=1, column=2, pady=(8, 0))
    conn_frame.columnconfigure(1, weight=1)

    cfg_frame = ttk.LabelFrame(outer, text="Настройки", padding=12)
    cfg_frame.pack(fill="both", expand=True, pady=(12, 0))

    c = cfg()
    port_var = tk.StringVar(value=str(c.port))
    url_var = tk.StringVar(value=c.ai_toolkit_url)
    cmd_var = tk.StringVar(value=c.start_command)
    cwd_var = tk.StringVar(value=c.start_cwd)
    datasets_var = tk.StringVar(value=c.datasets_dir)
    auto_var = tk.BooleanVar(value=c.autostart_windows)

    def row(label, var, r, browse=None):
        ttk.Label(cfg_frame, text=label).grid(row=r, column=0, sticky="w", pady=5)
        ttk.Entry(cfg_frame, textvariable=var).grid(row=r, column=1, sticky="ew", padx=8, pady=5)
        if browse:
            ttk.Button(cfg_frame, text="...", width=4, command=browse).grid(row=r, column=2, pady=5)

    def browse_cmd():
        p = filedialog.askopenfilename(title="Выбери .bat/.cmd/.exe для запуска AI Toolkit",
                                       filetypes=[("Programs", "*.bat *.cmd *.exe"), ("All files", "*.*")])
        if p:
            cmd_var.set(f'"{p}"')
            if not cwd_var.get():
                cwd_var.set(str(Path(p).parent))

    def browse_cwd():
        p = filedialog.askdirectory(title="Папка AI Toolkit")
        if p:
            cwd_var.set(p)
            ds = Path(p) / "datasets"
            if ds.exists() and not datasets_var.get():
                datasets_var.set(str(ds))

    def browse_datasets():
        p = filedialog.askdirectory(title="Папка datasets AI Toolkit")
        if p:
            datasets_var.set(p)

    row("Порт сервера", port_var, 0)
    row("AI Toolkit URL", url_var, 1)
    row("Команда запуска", cmd_var, 2, browse_cmd)
    row("Рабочая папка", cwd_var, 3, browse_cwd)
    row("Папка datasets", datasets_var, 4, browse_datasets)
    ttk.Checkbutton(cfg_frame, text="Запускать сервер вместе с Windows", variable=auto_var).grid(
        row=5, column=1, sticky="w", padx=8, pady=(8, 4)
    )
    cfg_frame.columnconfigure(1, weight=1)

    buttons = ttk.Frame(outer)
    buttons.pack(fill="x", pady=(12, 0))

    def copy_text(value):
        root.clipboard_clear()
        root.clipboard_append(value)
        root.update()

    def refresh_connection():
        c2 = cfg()
        ips = local_ipv4_addresses()
        # Prefer a Tailscale address (100.64.0.0/10) when present, otherwise LAN.
        chosen = next((ip for ip in ips if ip.startswith("100.")), ips[0])
        address_var.set(f"http://{chosen}:{c2.port}")
        key_var.set(c2.access_key)

    def save_settings():
        try:
            p = int(port_var.get().strip())
            if p < 1 or p > 65535:
                raise ValueError()
        except Exception:
            messagebox.showerror("Ошибка", "Порт должен быть от 1 до 65535.")
            return
        old = cfg()
        new = Config(
            host="0.0.0.0",
            port=p,
            access_key=old.access_key,
            ai_toolkit_url=url_var.get().strip() or "http://127.0.0.1:8675",
            start_command=cmd_var.get().strip(),
            start_cwd=cwd_var.get().strip(),
            datasets_dir=datasets_var.get().strip(),
            request_timeout=old.request_timeout,
            max_upload_mb=old.max_upload_mb,
            autostart_windows=bool(auto_var.get()),
        )
        parsed = urllib.parse.urlsplit(new.ai_toolkit_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            messagebox.showerror("Ошибка", "AI Toolkit URL должен указывать на localhost/127.0.0.1.")
            return
        update_config(new)
        ok, info = restart_http_server()
        refresh_connection()
        if ok:
            messagebox.showinfo("Готово", "Настройки сохранены. Сервер перезапущен.")
        else:
            messagebox.showerror("Ошибка запуска сервера", info)

    def test_toolkit():
        online = is_ai_toolkit_online()
        toolkit_var.set("AI Toolkit: ONLINE" if online else "AI Toolkit: OFFLINE")
        if not online:
            messagebox.showwarning("AI Toolkit", "AI Toolkit не отвечает на " + cfg().ai_toolkit_url)

    def launch_toolkit():
        ok, state = launch_ai_toolkit()
        if not ok:
            messagebox.showerror("Запуск AI Toolkit", state)
        else:
            toolkit_var.set("AI Toolkit: запуск запрошен...")
            root.after(2500, test_toolkit)

    def open_toolkit():
        webbrowser.open(cfg().ai_toolkit_url)

    ttk.Button(buttons, text="Сохранить", command=save_settings).pack(side="left")
    ttk.Button(buttons, text="Проверить AI Toolkit", command=test_toolkit).pack(side="left", padx=8)
    ttk.Button(buttons, text="Запустить AI Toolkit", command=launch_toolkit).pack(side="left")
    ttk.Button(buttons, text="Открыть UI", command=open_toolkit).pack(side="right")

    hint = ttk.Label(
        outer,
        text=(
            "На iPhone: Настройки → PC Remote Server → вставь «Адрес» и «Ключ» отсюда.\n"
            "Для доступа вне дома используй Tailscale IP. Порт AI Toolkit 8675 наружу открывать не нужно."
        ),
        justify="left",
    )
    hint.pack(anchor="w", pady=(14, 0))

    def periodic():
        online = is_ai_toolkit_online()
        toolkit_var.set("AI Toolkit: ONLINE" if online else "AI Toolkit: OFFLINE")
        if SERVER is not None:
            server_var.set(f"Сервер: ONLINE • порт {cfg().port}")
        else:
            server_var.set("Сервер: OFFLINE")
        refresh_connection()
        root.after(3000, periodic)

    def close():
        stop_http_server()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    ok, info = start_http_server()
    server_var.set(f"Сервер: ONLINE • порт {cfg().port}" if ok else f"Сервер: ERROR • {info}")
    refresh_connection()
    periodic()
    root.mainloop()


if __name__ == "__main__":
    run_gui()
