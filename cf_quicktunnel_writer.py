#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Cloudflared Quick Tunnel URL Writer with:
- built-in stdlib HTTP pull endpoint (for LAN pull)
- optional FTPS upload to stable public web space (Greensta) for Internet pull
- static, read-only file server for selected ComfyUI output subfolders
- .env support (stdlib-only) for credentials and configuration
- NEW: Remote restart via FTPS flag file + local backend health auto-restart

Why:
- Quick Tunnels rotate hostnames. To provide a stable pull URL for slAIdshow,
  we additionally upload tunnel_url.json/txt to a fixed HTTPS location on your web host.
- This bypasses CGNAT and avoids router port forwards.
- Additionally, we expose selected ComfyUI output directories via the same server
  so you can fetch generated 3D/mesh/video artifacts from other devices (and over the tunnel).
- NEW: If your backend (e.g. ComfyUI on 127.0.0.1:8188) stops responding, the health checker
  can restart cloudflared automatically. You can also trigger a restart remotely by uploading
  a simple flag file to your FTPS directory (no RDP/SSH required).

Endpoints served locally (optional for LAN):
  GET /bridge/tunnel_url.json
  GET /bridge/tunnel_url.txt
  GET /health
  GET /

Static file serving (read-only, configurable; defaults to ComfyUI's output/<subfolder>):
  - GET /files/3d/<path>     -> serves from FILES_3D_DIR (e.g., output/3d)
  - GET /files/mesh/<path>   -> serves from FILES_MESH_DIR (e.g., output/mesh)
  - GET /files/video/<path>  -> serves from FILES_VIDEO_DIR (e.g., output/video)
  - Directory listing (optional): /files/<sub>?list=json returns JSON list if enabled

Features:
  - Safe path resolution (prevents directory traversal)
  - MIME types for common formats (.mp4, .obj, .ply, .glb, .gltf, .stl, .fbx, .zip, .png, .jpg, .json, etc.)
  - HTTP Range requests (206) for large video files (configurable)
  - ETag and Last-Modified headers; basic caching
  - CORS toggle for all endpoints (bridge and files)
  - Health-checker with auto-restart
  - Remote restart via FTPS flag file

FTPS upload (stdlib-only via ftplib.FTP_TLS):
  - When enabled, after each URL change, upload JSON and TXT to the remote dir.
  - Creates subdirectories if missing (best-effort).

.env usage:

- Place a .env file next to this script (or provide --env-file) with lines like:
    FTPS_ENABLE=true
    FTPS_HOST=
    FTPS_USER=
    FTPS_PASS=
    FTPS_DIR=
    COMFY_URL=http://127.0.0.1:8188
    HTTP_HOST=0.0.0.0
    HTTP_PORT=8799
    HTTP_CORS=false
    CLOUDFLARED=cloudflared
    OUT_DIR=./bridge_output
    EDGE_PROTOCOL=http2
    FILES_ENABLE=true
    FILES_ROOT=./output
    FILES_3D_DIR=./output/3d
    FILES_MESH_DIR=./output/mesh
    FILES_VIDEO_DIR=./output/video
    FILES_INDEX=true
    FILES_RANGE=true

    # NEW: Health-check + remote-restart
    HEALTH_TARGET=http://127.0.0.1:8188
    HEALTH_INTERVAL=15
    HEALTH_THRESHOLD=3
    FTPS_RESTART_FLAG=restart_comfy.flag
    FTPS_CHECK_INTERVAL=30

- Values may be quoted: KEY="value with spaces"

Priority:
- CLI arguments override environment variables.
- .env is loaded into os.environ before full CLI parsing.
- If --env-file is omitted, the loader auto-discovers .env next to the script,
  then in current working directory.

Prerequisites:

-> Place this file and the .env.example into the folder where you have your cloudflared.exe
-> Fill .env.example with your host adress and keys and save it as .env

Typical start:

with .env in the same directory:

    python cf_quicktunnel_writer.py

without .env:

  python cf_quicktunnel_writer.py ^
    --cloudflared "cloudflared-windows-amd64.exe" ^
    --comfy-url "http://127.0.0.1:8188" ^
    --out-dir "C:\Users\Administrator\Documents\Arbeiten\0000_DEV\ComfyUI\output" ^
    --http-host 0.0.0.0 --http-port 8799 ^
    --files-enable --files-root "C:\Users\...\ComfyUI\output" ^
    --files-3d-dir "C:\Users\...\ComfyUI\output\3d" ^
    --files-mesh-dir "C:\Users\...\ComfyUI\output\mesh" ^
    --files-video-dir "C:\Users\...\ComfyUI\output\video" ^
    --files-index --files-range ^
    --ftps-enable ^
    --ftps-host "*****" ^
    --ftps-user "********" ^
    --ftps-dir "*****"

Security tips:
- Do NOT commit credentials. Prefer the .env file excluded via .gitignore, or use process env vars.
- FTPS uses TLS but still verify you trust the hosting.
- Static file endpoints are read-only and directory-traversal safe. Consider keeping FILES_ENABLE=false
  if you do not need public file access, or protect the tunnel with Cloudflare Access.

Remote restart how-to:
- Upload an empty file named FTPS_RESTART_FLAG (default: restart_comfy.flag) into FTPS_DIR.
- The watcher sees it within FTPS_CHECK_INTERVAL seconds, deletes it, and restarts cloudflared.
- New URL is written to tunnel_url.json/txt and uploaded as usual.

Test checklist:
1) Start cloudflared binary is reachable (or specify --cloudflared).
2) Start the script without FTPS to verify local endpoints:
   - curl http://127.0.0.1:8799/health
   - curl http://127.0.0.1:8799/bridge/tunnel_url.json
3) If FILES_ENABLE:
   - curl http://127.0.0.1:8799/files/video/?list=json
   - curl -I http://127.0.0.1:8799/files/video/test.mp4
   - curl -I http://127.0.0.1:8799/files/3d/model.obj
4) Enable FTPS and check uploads on URL change. Ensure remote path is correct.
5) Health: Stop ComfyUI temporarily; after HEALTH_THRESHOLD failures, the tunnel should auto-restart.
6) Remote flag: Upload restart_comfy.flag to FTPS_DIR; the tunnel should restart and upload a new URL.

"""

import argparse
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple, Dict, Any, List

# ========== Cloudflared URL detection ==========
TRYCF_RE = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com", re.IGNORECASE)

# ========== .env loader (stdlib-only) ==========

def _strip_quotes(value: str) -> str:
    """Strip wrapping single or double quotes if present."""
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value

def load_env_file(path: str, overwrite: bool = False) -> int:
    """
    Load KEY=VALUE pairs from a .env file into os.environ.
    - Supports comments (#), blank lines, quoted values.
    - overwrite=False means: do not override existing environment keys.
    Returns number of keys set/updated.
    """
    set_count = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                val = _strip_quotes(val)
                if not key:
                    continue
                if overwrite or (key not in os.environ):
                    os.environ[key] = val
                    set_count += 1
        return set_count
    except FileNotFoundError:
        return 0
    except Exception as e:
        print(f"[env] failed to load {path}: {e}", flush=True)
        return 0

def auto_discover_env_file(script_dir: str, cwd: str) -> Optional[str]:
    """
    Returns the first existing .env path among:
      1) script_dir/.env
      2) cwd/.env (if different from script_dir)
    """
    candidates = [
        os.path.join(script_dir, ".env"),
    ]
    if os.path.abspath(cwd) != os.path.abspath(script_dir):
        candidates.append(os.path.join(cwd, ".env"))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None

def env_flag_truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")

# ========== Helpers ==========

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def http_date(ts: float) -> str:
    """Return HTTP-date string for Last-Modified header."""
    from email.utils import formatdate
    return formatdate(timeval=ts, usegmt=True)

def atomic_write(path: str, data: bytes):
    """Atomically write bytes to a file path (safe replace)."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    base = os.path.basename(path)
    fd, tmppath = tempfile.mkstemp(prefix=base + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmppath, path)
    except Exception:
        try:
            os.unlink(tmppath)
        except Exception:
            pass
        raise

def read_json_file(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def read_text_file(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None

def parse_url_parts(url: str) -> Tuple[str, str, int]:
    """Parse scheme, host, port with stdlib only."""
    scheme = "https" if url.lower().startswith("https://") else "http"
    rest = url.split("://", 1)[1] if "://" in url else url
    hostport = rest.split("/", 1)[0]
    if ":" in hostport:
        host, port_s = hostport.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            port = 443 if scheme == "https" else 80
    else:
        host = hostport
        port = 443 if scheme == "https" else 80
    return scheme, host, port

def safe_join(base_dir: str, rel_path: str) -> Optional[str]:
    """
    Safely join base_dir with a user-supplied relative path.
    Prevent directory traversal by ensuring result stays within base_dir.
    """
    rel_path = rel_path.split("?", 1)[0].split("#", 1)[0]
    rel_path = rel_path.lstrip("/\\")
    norm = os.path.normpath(os.path.join(base_dir, rel_path))
    base_abs = os.path.abspath(base_dir)
    norm_abs = os.path.abspath(norm)
    if os.path.commonpath([base_abs]) != os.path.commonpath([base_abs, norm_abs]):
        return None
    return norm_abs

def guess_mime_type(filename: str) -> str:
    """
    Best-effort MIME type mapping for common 3D/video/mesh formats.
    Falls back to application/octet-stream.
    """
    ext = os.path.splitext(filename.lower())[1]
    if ext in (".mp4",):
        return "video/mp4"
    if ext in (".webm",):
        return "video/webm"
    if ext in (".mov",):
        return "video/quicktime"
    if ext in (".mkv",):
        return "video/x-matroska"
    if ext in (".png",):
        return "image/png"
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext in (".gif",):
        return "image/gif"
    if ext in (".bmp",):
        return "image/bmp"
    if ext in (".json",):
        return "application/json; charset=utf-8"
    if ext in (".txt", ".log"):
        return "text/plain; charset=utf-8"
    if ext in (".zip",):
        return "application/zip"
    if ext in (".tar", ".gz", ".tgz", ".bz2", ".xz"):
        return "application/octet-stream"
    # 3D/mesh
    if ext in (".obj",):
        return "model/obj"
    if ext in (".ply",):
        return "application/octet-stream"
    if ext in (".stl",):
        return "model/stl"
    if ext in (".fbx",):
        return "application/octet-stream"
    if ext in (".glb",):
        return "model/gltf-binary"
    if ext in (".gltf",):
        return "model/gltf+json"
    if ext in (".usdz", ".usd", ".usda", ".usdc"):
        return "application/octet-stream"
    return "application/octet-stream"

# ========== FTPS Upload (stdlib: ftplib) ==========
from ftplib import FTP_TLS, error_perm, all_errors as ftplib_errors
from contextlib import contextmanager

@contextmanager
def ftps_connect(host: str, user: str, password: str, timeout: float = 20.0):
    """
    Context manager to connect/login to FTPS (explicit TLS).
    Performs PROT P to encrypt data channel.
    """
    ftps = FTP_TLS()
    ftps.connect(host=host, timeout=timeout)
    ftps.auth()
    ftps.prot_p()
    ftps.login(user=user, passwd=password)
    try:
        yield ftps
    finally:
        try:
            ftps.quit()
        except Exception:
            try:
                ftps.close()
            except Exception:
                pass

def _ftps_mkdirs(ftps: FTP_TLS, remote_dir: str):
    """
    Create remote_dir and parents if missing. Tries to CWD stepwise and MKD on failure.
    Works for typical FTP servers; silently continues if dirs exist.
    """
    if not remote_dir or remote_dir == "/":
        return
    parts = [p for p in remote_dir.strip("/").split("/") if p]
    path_so_far = ""
    try:
        ftps.cwd("/")
    except Exception:
        pass
    for p in parts:
        path_so_far = path_so_far + "/" + p
        try:
            ftps.cwd(path_so_far)
        except Exception:
            try:
                ftps.mkd(path_so_far)
            except ftplib_errors as e:
                msg = str(e).lower()
                if "exists" not in msg and "file unavailable" not in msg:
                    raise
            ftps.cwd(path_so_far)

def ftps_upload_file(host: str, user: str, password: str, local_path: str, remote_dir: str, remote_filename: Optional[str] = None, timeout: float = 20.0):
    """
    Upload local_path to host:/remote_dir/remote_filename via FTPS with binary transfer.
    Ensures remote_dir exists (best-effort).
    """
    if remote_filename is None:
        remote_filename = os.path.basename(local_path)
    with ftps_connect(host, user, password, timeout=timeout) as ftps:
        _ftps_mkdirs(ftps, remote_dir)
        if remote_dir and remote_dir != "/":
            ftps.cwd(remote_dir)
        with open(local_path, "rb") as lf:
            ftps.storbinary(f"STOR {remote_filename}", lf)

def upload_with_retries(host: str, user: str, password: str, local_path: str, remote_dir: str, remote_filename: Optional[str] = None, retries: int = 5, base_delay: float = 1.0):
    """
    Retry FTPS upload with exponential backoff.
    """
    attempt = 0
    last_exc = None
    while attempt < retries:
        try:
            ftps_upload_file(host, user, password, local_path, remote_dir, remote_filename)
            print(f"[ftps] uploaded {local_path} -> {host}:{remote_dir}/{remote_filename or os.path.basename(local_path)}", flush=True)
            return True
        except Exception as e:
            last_exc = e
            delay = base_delay * (1.7 ** attempt)
            print(f"[ftps] upload failed (attempt {attempt+1}/{retries}): {e}; retrying in {delay:.1f}s", flush=True)
            time.sleep(delay)
            attempt += 1
    print(f"[ftps] upload permanently failed: {last_exc}", flush=True)
    return False

# ========== Cloudflared writer + Restart Helpers ==========

class TunnelWriter:
    """
    Manage cloudflared subprocess, detect public URL, persist JSON/TXT, and optionally FTPS-upload.
    Also exposes a restart() to kill and relaunch cloudflared on demand.
    """

    def __init__(self, cf_bin: str, comfy_url: str, out_dir: str, protocol: str = "http2",
                 ftps_enable: bool = False, ftps_host: str = "", ftps_user: str = "", ftps_pass: str = "",
                 ftps_dir: str = "", ftps_retries: int = 5):
        self.cf_bin = cf_bin
        self.comfy_url = comfy_url
        self.out_dir = out_dir
        self.protocol = protocol
        self.proc: Optional[subprocess.Popen] = None
        self.stop_evt = threading.Event()
        self.current_url = ""
        self.backoff = 2.0

        # protect proc operations
        self._proc_lock = threading.Lock()

        # FTPS config
        self.ftps_enable = ftps_enable
        self.ftps_host = ftps_host
        self.ftps_user = ftps_user
        self.ftps_pass = ftps_pass
        self.ftps_dir = ftps_dir.rstrip("/") if ftps_dir else ""
        self.ftps_retries = ftps_retries

        os.makedirs(self.out_dir, exist_ok=True)
        self.json_path = os.path.join(self.out_dir, "tunnel_url.json")
        self.txt_path = os.path.join(self.out_dir, "tunnel_url.txt")

    def write_urls_local(self, url: str):
        """Persist URL to .json and .txt atomically (local)."""
        payload = {"url": url, "updated_at": utc_now_iso()}
        data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        data_txt = (url or "").encode("utf-8")
        atomic_write(self.json_path, data_json)
        atomic_write(self.txt_path, data_txt)
        print(f"[writer] wrote {self.json_path} and {self.txt_path}", flush=True)

    def upload_remote_if_enabled(self):
        """Upload local JSON and TXT to FTPS target when enabled."""
        if not self.ftps_enable:
            return
        if not (self.ftps_host and self.ftps_user and self.ftps_pass and self.ftps_dir):
            print("[ftps] missing credentials or remote dir; skipping upload", flush=True)
            return
        upload_with_retries(self.ftps_host, self.ftps_user, self.ftps_pass,
                            self.json_path, self.ftps_dir, remote_filename="tunnel_url.json",
                            retries=self.ftps_retries)
        upload_with_retries(self.ftps_host, self.ftps_user, self.ftps_pass,
                            self.txt_path, self.ftps_dir, remote_filename="tunnel_url.txt",
                            retries=self.ftps_retries)

    def _spawn_cloudflared(self) -> bool:
        cmd = [
            self.cf_bin,
            "tunnel",
            "--no-autoupdate",
            "--protocol",
            self.protocol,
            "--url",
            self.comfy_url,
        ]
        print(f"[cloudflared] starting: {' '.join(cmd)}", flush=True)
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                universal_newlines=True,
            )
            return True
        except Exception as e:
            print(f"[cloudflared] spawn error: {e}", flush=True)
            return False

    def run_once(self):
        ok = False
        with self._proc_lock:
            ok = self._spawn_cloudflared()
            proc = self.proc
        if not ok or proc is None:
            return False

        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if self.stop_evt.is_set():
                    break
                line = line.rstrip("\r\n")
                print(f"[cloudflared] {line}", flush=True)
                m = TRYCF_RE.search(line)
                if m:
                    url = m.group(0)
                    if url != self.current_url:
                        self.current_url = url
                        print(f"[cloudflared] detected tunnel URL: {url}", flush=True)
                        try:
                            self.write_urls_local(url)
                            self.upload_remote_if_enabled()
                        except Exception as we:
                            print(f"[writer] error after detection: {we}", flush=True)
        except Exception as e:
            print(f"[cloudflared] read error: {e}", flush=True)

        # Wait for exit
        try:
            rc = proc.wait(timeout=2)
        except Exception:
            rc = None
        print(f"[cloudflared] exited rc={rc}", flush=True)
        return True

    def run_forever(self):
        while not self.stop_evt.is_set():
            _ = self.run_once()
            if self.stop_evt.is_set():
                break
            # Backoff and retry on crash/failure
            t = self.backoff
            self.backoff = min(self.backoff * 1.5, 30.0)
            for _ in range(int(t / 0.1)):
                if self.stop_evt.is_set():
                    break
                time.sleep(0.1)

    def is_running(self) -> bool:
        try:
            with self._proc_lock:
                return self.proc is not None and self.proc.poll() is None
        except Exception:
            return False

    def restart(self, reason: str = "manual"):
        """
        Kill and relaunch cloudflared quickly. Safe to call from watcher threads.
        """
        print(f"[restart] requested ({reason})", flush=True)
        with self._proc_lock:
            try:
                if self.proc and self.proc.poll() is None:
                    print("[restart] terminating current cloudflared...", flush=True)
                    self.proc.terminate()
            except Exception as e:
                print(f"[restart] terminate error: {e}", flush=True)
        # Allow run_forever() loop to notice exit and respawn sooner by resetting backoff
        self.backoff = 2.0

    def stop(self):
        self.stop_evt.set()
        with self._proc_lock:
            try:
                if self.proc and self.proc.poll() is None:
                    self.proc.terminate()
            except Exception:
                pass

# ========== HTTP server (bridge + static files) ==========

class BridgeRequestHandler(BaseHTTPRequestHandler):
    """
    Serves:
      - Bridge endpoints:
        - GET /bridge/tunnel_url.json
        - GET /bridge/tunnel_url.txt
        - GET /health
        - GET /
      - Static files (read-only):
        - GET /files/3d/<path>    -> FILES_3D_DIR
        - GET /files/mesh/<path>  -> FILES_MESH_DIR
        - GET /files/video/<path> -> FILES_VIDEO_DIR
        - Optional listing: /files/<sub>?list=json
    """

    # Bridge config (populated from main)
    out_dir: str = "."
    json_path: str = "tunnel_url.json"
    txt_path: str = "tunnel_url.txt"
    enable_cors: bool = False
    writer_ref: Optional[TunnelWriter] = None

    # Files config (populated from main)
    files_enable: bool = False
    files_index: bool = False
    files_range: bool = False
    dirs_map: Dict[str, str] = {}

    server_version = "BridgeServer/1.4"
    sys_version = ""

    def _set_common_headers(self, status: int, content_type: str, extra: Optional[Dict[str, str]] = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if self.enable_cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
            self.send_header("Accept-Ranges", "bytes")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        if self.enable_cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        self.end_headers()

    # -------- Helpers for static files --------

    def _parse_files_request(self) -> Optional[Tuple[str, str, Dict[str, str]]]:
        if not self.path.startswith("/files/"):
            return None
        if "?" in self.path:
            path_part, query = self.path.split("?", 1)
        else:
            path_part, query = self.path, ""
        parts = path_part.rstrip("/").split("/", 3)
        if len(parts) < 3:
            return None
        sub = parts[2] if len(parts) >= 3 else ""
        rel = parts[3] if len(parts) >= 4 else ""
        qparams: Dict[str, str] = {}
        if query:
            for kv in query.split("&"):
                if not kv:
                    continue
                if "=" in kv:
                    k, v = kv.split("=", 1)
                else:
                    k, v = kv, ""
                qparams[k] = v
        return (sub, rel, qparams)

    def _list_directory_json(self, base_dir: str, rel_path: str) -> bytes:
        target = safe_join(base_dir, rel_path or ".")
        if target is None:
            return json.dumps({"error": "forbidden"}).encode("utf-8")
        if not os.path.isdir(target):
            return json.dumps({"error": "not_directory"}).encode("utf-8")
        items: List[Dict[str, Any]] = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    try:
                        st = entry.stat()
                        items.append({
                            "name": entry.name,
                            "type": "dir" if entry.is_dir() else "file",
                            "size": int(st.st_size),
                            "mtime": utc_now_iso() if not st.st_mtime else datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        })
                    except Exception:
                        items.append({"name": entry.name, "type": "unknown"})
        except Exception as e:
            return json.dumps({"error": str(e)}).encode("utf-8")
        return json.dumps({"path": rel_path, "items": items}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _send_file_with_optional_range(self, fs_path: str, mime: str):
        try:
            st = os.stat(fs_path)
        except FileNotFoundError:
            self._set_common_headers(HTTPStatus.NOT_FOUND, "application/json")
            self.wfile.write(b'{"error":"not_found"}')
            return
        except Exception as e:
            self._set_common_headers(HTTPStatus.INTERNAL_SERVER_ERROR, "application/json")
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        size = st.st_size
        mtime = st.st_mtime
        last_mod = http_date(mtime)
        etag = f'W/"{size:x}-{int(mtime):x}"'

        inm = self.headers.get("If-None-Match")
        ims = self.headers.get("If-Modified-Since")
        if inm and inm == etag:
            self._set_common_headers(HTTPStatus.NOT_MODIFIED, mime, {"ETag": etag, "Last-Modified": last_mod})
            return
        if ims and ims == last_mod:
            self._set_common_headers(HTTPStatus.NOT_MODIFIED, mime, {"ETag": etag, "Last-Modified": last_mod})
            return

        range_header = self.headers.get("Range")
        if self.files_range and range_header and range_header.startswith("bytes="):
            try:
                token = range_header[len("bytes="):]
                if "," in token:
                    raise ValueError("multiple ranges not supported")
                start_s, end_s = token.split("-", 1)
                if start_s == "":
                    suffix = int(end_s)
                    if suffix <= 0:
                        raise ValueError("invalid suffix")
                    start = max(0, size - suffix)
                    end = size - 1
                else:
                    start = int(start_s)
                    end = int(end_s) if end_s else size - 1
                if start < 0 or end < start or end >= size:
                    raise ValueError("out of range")
            except Exception:
                self._set_common_headers(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "application/json", {
                    "Content-Range": f"bytes */{size}",
                    "ETag": etag,
                    "Last-Modified": last_mod,
                })
                self.wfile.write(b'{"error":"range_not_satisfiable"}')
                return

            length = end - start + 1
            headers = {
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(length),
                "ETag": etag,
                "Last-Modified": last_mod,
                "Cache-Control": "public, max-age=60",
            }
            self._set_common_headers(HTTPStatus.PARTIAL_CONTENT, mime, headers)
            try:
                with open(fs_path, "rb") as f:
                    f.seek(start)
                    to_send = length
                    bufsize = 1024 * 256
                    while to_send > 0:
                        chunk = f.read(min(bufsize, to_send))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        to_send -= len(chunk)
            except BrokenPipeError:
                pass
            except Exception as e:
                sys.stdout.write(f"[http] error sending range: {e}\n")
            return

        headers = {
            "Content-Length": str(size),
            "ETag": etag,
            "Last-Modified": last_mod,
            "Cache-Control": "public, max-age=60",
        }
        self._set_common_headers(HTTPStatus.OK, mime, headers)
        try:
            with open(fs_path, "rb") as f:
                bufsize = 1024 * 256
                while True:
                    chunk = f.read(bufsize)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except BrokenPipeError:
            pass
        except Exception as e:
            sys.stdout.write(f"[http] error sending file: {e}\n")

    def do_GET(self):
        try:
            if self.path == "/":
                self._set_common_headers(HTTPStatus.OK, "text/plain; charset=utf-8")
                body = (
                    "Cloudflared Quick Tunnel Bridge\n"
                    "Bridge Endpoints:\n"
                    "  GET /bridge/tunnel_url.json\n"
                    "  GET /bridge/tunnel_url.txt\n"
                    "  GET /health\n"
                    "\n"
                    "Static File Endpoints (read-only):\n"
                    "  GET /files/3d/<path>\n"
                    "  GET /files/mesh/<path>\n"
                    "  GET /files/video/<path>\n"
                    "  Optional listing: /files/<sub>?list=json\n"
                )
                self.wfile.write(body.encode("utf-8"))
                return

            if self.path.startswith("/bridge/tunnel_url.json"):
                data = read_json_file(self.json_path)
                if data is None:
                    self._set_common_headers(HTTPStatus.NOT_FOUND, "application/json")
                    self.wfile.write(b'{"error":"not_found"}')
                    return
                payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._set_common_headers(HTTPStatus.OK, "application/json", {"Cache-Control": "no-cache"})
                self.wfile.write(payload)
                return

            if self.path.startswith("/bridge/tunnel_url.txt"):
                txt = read_text_file(self.txt_path)
                if txt is None:
                    self._set_common_headers(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
                    self.wfile.write(b"")
                    return
                self._set_common_headers(HTTPStatus.OK, "text/plain; charset=utf-8", {"Cache-Control": "no-cache"})
                self.wfile.write(txt.encode("utf-8"))
                return

            if self.path.startswith("/health"):
                data = read_json_file(self.json_path) or {}
                url = data.get("url") if isinstance(data, dict) else None
                running = False
                if self.writer_ref is not None:
                    running = self.writer_ref.is_running()
                resp = {
                    "status": "ok",
                    "cloudflared_running": bool(running),
                    "url": url or "",
                    "updated_at": data.get("updated_at") if isinstance(data, dict) else None,
                }
                payload = json.dumps(resp, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._set_common_headers(HTTPStatus.OK, "application/json", {"Cache-Control": "no-cache"})
                self.wfile.write(payload)
                return

            files_req = self._parse_files_request()
            if files_req and self.files_enable:
                sub, rel, q = files_req
                sub = (sub or "").lower()
                if sub not in self.dirs_map:
                    self._set_common_headers(HTTPStatus.NOT_FOUND, "application/json")
                    self.wfile.write(b'{"error":"unknown_subdir"}')
                    return
                base_dir = self.dirs_map[sub]
                if not base_dir:
                    self._set_common_headers(HTTPStatus.NOT_FOUND, "application/json")
                    self.wfile.write(b'{"error":"subdir_not_configured"}')
                    return

                if self.files_index and (rel == "" or self.path.rstrip("/").endswith(f"/files/{sub}") or q.get("list", "").lower() == "json"):
                    payload = self._list_directory_json(base_dir, rel)
                    self._set_common_headers(HTTPStatus.OK, "application/json", {"Cache-Control": "no-cache"})
                    self.wfile.write(payload)
                    return

                target = safe_join(base_dir, rel)
                if target is None:
                    self._set_common_headers(HTTPStatus.FORBIDDEN, "application/json")
                    self.wfile.write(b'{"error":"forbidden"}')
                    return
                if os.path.isdir(target):
                    self._set_common_headers(HTTPStatus.FORBIDDEN, "application/json")
                    self.wfile.write(b'{"error":"directory_listing_disabled"}')
                    return
                if not os.path.isfile(target):
                    self._set_common_headers(HTTPStatus.NOT_FOUND, "application/json")
                    self.wfile.write(b'{"error":"not_found"}')
                    return
                mime = guess_mime_type(target)
                self._send_file_with_optional_range(target, mime)
                return

            self._set_common_headers(HTTPStatus.NOT_FOUND, "application/json")
            self.wfile.write(b'{"error":"not_found"}')
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, explain=str(e))
            except Exception:
                pass

    def log_message(self, fmt, *args):
        sys.stdout.write("[http] " + (fmt % args) + "\n")

class HttpServerThread(threading.Thread):
    def __init__(self, host: str, port: int, handler_cls: type):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.handler_cls = handler_cls
        self.httpd: Optional[ThreadingHTTPServer] = None
        self._stopped = threading.Event()

    def run(self):
        try:
            ThreadingHTTPServer.allow_reuse_address = True
            self.httpd = ThreadingHTTPServer((self.host, self.port), self.handler_cls)
            sa = self.httpd.socket.getsockname()
            print(f"[http] serving on {sa[0]}:{sa[1]}", flush=True)
            self.httpd.serve_forever(poll_interval=0.5)
        except OSError as e:
            print(f"[http] failed to bind {self.host}:{self.port} -> {e}", flush=True)
        except Exception as e:
            print(f"[http] server error: {e}", flush=True)
        finally:
            self._stopped.set()
            print("[http] server thread exited", flush=True)

    def stop(self):
        try:
            if self.httpd:
                self.httpd.shutdown()
                self.httpd.server_close()
        except Exception:
            pass
        try:
            with socket.create_connection((self.host, self.port), timeout=0.2):
                pass
        except Exception:
            pass
        self._stopped.wait(timeout=3.0)

# ========== Health + Remote-Flag Watcher Threads ==========

def _tcp_connect(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def _http_head(host: str, port: int, timeout: float = 2.0, path: str = "/") -> bool:
    """
    Very small HTTP HEAD using sockets to avoid external deps.
    Returns True on 200..399.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            req = f"HEAD {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
            s.sendall(req.encode("ascii"))
            s.settimeout(timeout)
            data = s.recv(1024)
            if not data:
                return False
            first = data.split(b"\r\n", 1)[0].decode("latin1", errors="ignore")
            return (" 200 " in first) or (" 204 " in first) or (" 301 " in first) or (" 302 " in first) or (" 307 " in first) or (" 308 " in first)
    except Exception:
        return False

def _parse_host_port_from_url(url: str) -> Tuple[str, int, str]:
    """
    Return (host, port, path) for http/https URLs.
    """
    scheme = "https" if url.lower().startswith("https://") else "http"
    rest = url.split("://", 1)[1] if "://" in url else url
    hostport_and_path = rest.split("/", 1)
    hostport = hostport_and_path[0]
    path = "/" + hostport_and_path[1] if len(hostport_and_path) > 1 else "/"
    if ":" in hostport:
        host, port_s = hostport.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            port = 443 if scheme == "https" else 80
    else:
        host = hostport
        port = 443 if scheme == "https" else 80
    return host, port, path

class HealthWatcher(threading.Thread):
    """
    Periodically checks a local HTTP backend and restarts the tunnel if it seems unhealthy.
    """
    def __init__(self, target_url: str, interval: int, threshold: int, tw: TunnelWriter):
        super().__init__(daemon=True)
        self.target_url = target_url.strip()
        self.interval = max(5, int(interval))
        self.threshold = max(1, int(threshold))
        self.tw = tw
        self._fails = 0
        self._stop = threading.Event()

    def run(self):
        if not self.target_url:
            return
        host, port, path = _parse_host_port_from_url(self.target_url)
        print(f"[health] watching {self.target_url} (interval={self.interval}s, threshold={self.threshold})", flush=True)
        while not self._stop.is_set():
            ok = _tcp_connect(host, port, timeout=1.5) and _http_head(host, port, timeout=2.0, path=path)
            if ok:
                if self._fails:
                    print("[health] backend OK again; reset fail counter", flush=True)
                self._fails = 0
            else:
                self._fails += 1
                print(f"[health] backend check failed ({self._fails}/{self.threshold})", flush=True)
                if self._fails >= self.threshold:
                    self._fails = 0
                    self.tw.restart(reason="health")
            for _ in range(self.interval * 10):
                if self._stop.is_set():
                    break
                time.sleep(0.1)

    def stop(self):
        self._stop.set()

class FtpsFlagWatcher(threading.Thread):
    """
    Periodically checks FTPS_DIR for a restart flag file. If present, deletes it and restarts the tunnel.
    """
    def __init__(self, enabled: bool, host: str, user: str, password: str, remote_dir: str, flag_name: str, interval: int, tw: TunnelWriter):
        super().__init__(daemon=True)
        self.enabled = enabled
        self.host = host
        self.user = user
        self.password = password
        self.remote_dir = (remote_dir or "").rstrip("/")
        self.flag_name = flag_name.strip()
        self.interval = max(10, int(interval))
        self.tw = tw
        self._stop = threading.Event()

    def run(self):
        if not (self.enabled and self.host and self.user and self.password and self.remote_dir and self.flag_name):
            return
        print(f"[flag] watching {self.host}:{self.remote_dir}/{self.flag_name} every {self.interval}s", flush=True)
        while not self._stop.is_set():
            try:
                with ftps_connect(self.host, self.user, self.password, timeout=20.0) as ftps:
                    try:
                        ftps.cwd(self.remote_dir)
                    except Exception as e:
                        print(f"[flag] cwd failed: {e}", flush=True)
                        raise
                    files = []
                    try:
                        ftps.retrlines('NLST', files.append)
                    except Exception as e:
                        print(f"[flag] NLST failed: {e}", flush=True)
                        files = []
                    if self.flag_name in files:
                        print("[flag] restart flag detected; deleting and restarting...", flush=True)
                        try:
                            ftps.delete(self.flag_name)
                        except Exception as e:
                            print(f"[flag] delete failed (continuing): {e}", flush=True)
                        self.tw.restart(reason="ftps_flag")
            except Exception as e:
                print(f"[flag] check error: {e}", flush=True)
            for _ in range(self.interval * 10):
                if self._stop.is_set():
                    break
                time.sleep(0.1)

    def stop(self):
        self._stop.set()

# ========== Utilities ==========

def find_default_cloudflared(script_path: str) -> str:
    base = os.path.dirname(os.path.abspath(script_path))
    candidates = [
        os.path.join(base, "cloudflared-windows-amd64.exe"),
        os.path.join(base, "cloudflared.exe"),
        "cloudflared",
    ]
    for c in candidates:
        if c == "cloudflared":
            return c
        if os.path.isfile(c):
            return c
    return "cloudflared"

# ========== Main ==========

def parse_stage1_args(argv=None):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--env-file", type=str, default="", help="Path to .env file to load before full parsing")
    p.add_argument("--no-auto-env", action="store_true", help="Disable auto-discovery of .env")
    return p.parse_known_args(argv)

def parse_stage2_args(argv=None):
    p = argparse.ArgumentParser(description="Cloudflared Quick Tunnel URL Writer with LAN HTTP endpoint, optional FTPS upload, static file serving, .env support, health auto-restart, and remote FTPS restart flag")
    # Primary settings
    p.add_argument("--cloudflared", type=str, default=os.getenv("CLOUDFLARED", ""), help="Path to cloudflared binary (auto-discover if empty or 'cloudflared')")
    p.add_argument("--comfy-url", type=str, default=os.getenv("COMFY_URL", "http://127.0.0.1:8188"), help="Local ComfyUI URL to expose")
    p.add_argument("--out-dir", type=str, default=os.getenv("OUT_DIR", ""), help="Directory to write tunnel_url.json and tunnel_url.txt")
    p.add_argument("--protocol", type=str, default=os.getenv("EDGE_PROTOCOL", "http2"), choices=["quic", "http2"], help="Cloudflared edge protocol")

    # HTTP endpoint (LAN)
    p.add_argument("--http-host", type=str, default=os.getenv("HTTP_HOST", "0.0.0.0"), help="HTTP bind host for pull endpoint")
    p.add_argument("--http-port", type=int, default=int(os.getenv("HTTP_PORT", "8799")), help="HTTP bind port for pull endpoint")
    default_cors_env = os.getenv("HTTP_CORS", "false")
    p.add_argument("--http-cors", action="store_true", default=env_flag_truthy(default_cors_env), help="Enable CORS (Access-Control-Allow-Origin: *)")

    # FTPS upload options
    default_ftps_enable = env_flag_truthy(os.getenv("FTPS_ENABLE", "false"))
    p.add_argument("--ftps-enable", action="store_true", default=default_ftps_enable, help="Enable FTPS upload on URL changes")
    p.add_argument("--ftps-host", type=str, default=os.getenv("FTPS_HOST", ""), help="FTPS host (e.g., web8.greensta.de)")
    p.add_argument("--ftps-user", type=str, default=os.getenv("FTPS_USER", ""), help="FTPS username")
    p.add_argument("--ftps-pass", type=str, default=os.getenv("FTPS_PASS", ""), help="FTPS password")
    p.add_argument("--ftps-dir", type=str, default=os.getenv("FTPS_DIR", ""), help="Remote directory path (e.g., /dev.betakontext.de/slAIdshow/bridge)")
    p.add_argument("--ftps-retries", type=int, default=int(os.getenv("FTPS_RETRIES", "5")), help="Retry count for FTPS uploads")

    # Static files options
    p.add_argument("--files-enable", action="store_true", default=env_flag_truthy(os.getenv("FILES_ENABLE", "false")), help="Enable static file serving under /files/*")
    p.add_argument("--files-root", type=str, default=os.getenv("FILES_ROOT", ""), help="Root directory for files (used for relative defaults)")
    p.add_argument("--files-3d-dir", type=str, default=os.getenv("FILES_3D_DIR", ""), help="Absolute or relative path to 3D files directory (default: <root>/3d)")
    p.add_argument("--files-mesh-dir", type=str, default=os.getenv("FILES_MESH_DIR", ""), help="Absolute or relative path to mesh files directory (default: <root>/mesh)")
    p.add_argument("--files-video-dir", type=str, default=os.getenv("FILES_VIDEO_DIR", ""), help="Absolute or relative path to video files directory (default: <root>/video)")
    p.add_argument("--files-index", action="store_true", default=env_flag_truthy(os.getenv("FILES_INDEX", "false")), help="Enable directory listing JSON via ?list=json")
    p.add_argument("--files-range", action="store_true", default=env_flag_truthy(os.getenv("FILES_RANGE", "true")), help="Enable HTTP Range (206) for file downloads (recommended for video)")

    # NEW: Health + FTPS flag watcher
    p.add_argument("--health-target", type=str, default=os.getenv("HEALTH_TARGET", ""), help="Local backend URL to check (e.g., http://127.0.0.1:8188)")
    p.add_argument("--health-interval", type=int, default=int(os.getenv("HEALTH_INTERVAL", "15")), help="Health check interval seconds")
    p.add_argument("--health-threshold", type=int, default=int(os.getenv("HEALTH_THRESHOLD", "3")), help="Consecutive failures before restart")

    p.add_argument("--ftps-restart-flag", type=str, default=os.getenv("FTPS_RESTART_FLAG", "restart_comfy.flag"), help="Filename in FTPS_DIR to trigger restart")
    p.add_argument("--ftps-check-interval", type=int, default=int(os.getenv("FTPS_CHECK_INTERVAL", "30")), help="Interval seconds for FTPS flag checking")

    # Keep stage1 flags too for help visibility
    p.add_argument("--env-file", type=str, default=os.getenv("ENV_FILE", ""), help="Path to .env file (already loaded if provided earlier)")
    p.add_argument("--no-auto-env", action="store_true", default=env_flag_truthy(os.getenv("NO_AUTO_ENV", "false")), help="Disable auto-discovery of .env")

    return p.parse_args(argv)

def _resolve_dir(path_value: str, root_fallback: str, sub: str, script_dir: str) -> str:
    if path_value:
        p = path_value
        if not os.path.isabs(p):
            p = os.path.abspath(os.path.join(script_dir, p))
        return p
    base = root_fallback or os.path.join(script_dir, "output")
    if not os.path.isabs(base):
        base = os.path.abspath(os.path.join(script_dir, base))
    return os.path.join(base, sub)

def main():
    # Stage 1: early parse for .env loading controls
    stage1_args, remaining = parse_stage1_args()

    # Load .env
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cwd = os.getcwd()
    env_loaded_from = None

    if stage1_args.env_file:
        count = load_env_file(stage1_args.env_file, overwrite=False)
        env_loaded_from = stage1_args.env_file if count >= 0 else None
        print(f"[env] loaded {count} keys from {stage1_args.env_file}", flush=True)
    elif not stage1_args.no_auto_env:
        auto_env = auto_discover_env_file(script_dir, cwd)
        if auto_env:
            count = load_env_file(auto_env, overwrite=False)
            env_loaded_from = auto_env if count >= 0 else None
            print(f"[env] auto-loaded {count} keys from {auto_env}", flush=True)
        else:
            print("[env] no .env discovered", flush=True)
    else:
        print("[env] auto-discovery disabled", flush=True)

    # Stage 2: full parse with defaults from os.environ
    args = parse_stage2_args(remaining)

    # Resolve cloudflared binary and out dir
    cf_bin = (args.cloudflared or "").strip() or find_default_cloudflared(__file__)
    out_dir = (args.out_dir or "").strip() or (os.path.dirname(os.path.abspath(__file__)) or ".")

    # Resolve file directories
    files_root = args.files_root.strip()
    if files_root and not os.path.isabs(files_root):
        files_root = os.path.abspath(os.path.join(script_dir, files_root))

    dir_3d = _resolve_dir(args.files_3d_dir.strip(), files_root, "3d", script_dir)
    dir_mesh = _resolve_dir(args.files_mesh_dir.strip(), files_root, "mesh", script_dir)
    dir_video = _resolve_dir(args.files_video_dir.strip(), files_root, "video", script_dir)

    # Mask sensitive info for logs
    masked_pass = "****" if args.ftps_pass else ""

    print(
        f"[main] cloudflared={cf_bin}, comfy={args.comfy_url}, out_dir={out_dir}, "
        f"http={args.http_host}:{args.http_port}, protocol={args.protocol}, cors={args.http_cors}, "
        f"files_enable={args.files_enable}, files_root={files_root or '(default: ./output)'}, "
        f"files_3d_dir={dir_3d}, files_mesh_dir={dir_mesh}, files_video_dir={dir_video}, "
        f"files_index={args.files_index}, files_range={args.files_range}, "
        f"ftps_enable={args.ftps_enable}, ftps_host={args.ftps_host}, ftps_user={args.ftps_user}, "
        f"ftps_pass={masked_pass}, ftps_dir={args.ftps_dir}, ftps_retries={args.ftps_retries}, "
        f"health_target={args.health_target or '(disabled)'}, health_interval={args.health_interval}, health_threshold={args.health_threshold}, "
        f"flag={args.ftps_restart_flag} every {args.ftps_check_interval}s, "
        f"env_file={(env_loaded_from or 'none')}",
        flush=True,
    )

    # Validate FTPS config if enabled
    if args.ftps_enable:
        missing = []
        if not args.ftps_host:
            missing.append("FTPS_HOST/--ftps-host")
        if not args.ftps_user:
            missing.append("FTPS_USER/--ftps-user")
        if not args.ftps_pass:
            missing.append("FTPS_PASS/--ftps-pass")
        if not args.ftps_dir:
            missing.append("FTPS_DIR/--ftps-dir")
        if missing:
            print(f"[main] error: --ftps-enable requires: {', '.join(missing)}", flush=True)
            sys.exit(2)

    # Prepare writer
    tw = TunnelWriter(
        cf_bin=cf_bin,
        comfy_url=args.comfy_url,
        out_dir=out_dir,
        protocol=args.protocol,
        ftps_enable=bool(args.ftps_enable),
        ftps_host=args.ftps_host.strip(),
        ftps_user=args.ftps_user.strip(),
        ftps_pass=args.ftps_pass,
        ftps_dir=args.ftps_dir.strip(),
        ftps_retries=args.ftps_retries,
    )

    # Prepare HTTP handler class with shared config
    BridgeRequestHandler.out_dir = out_dir
    BridgeRequestHandler.json_path = os.path.join(out_dir, "tunnel_url.json")
    BridgeRequestHandler.txt_path = os.path.join(out_dir, "tunnel_url.txt")
    BridgeRequestHandler.enable_cors = bool(args.http_cors)
    BridgeRequestHandler.writer_ref = tw

    # Static files setup
    BridgeRequestHandler.files_enable = bool(args.files_enable)
    BridgeRequestHandler.files_index = bool(args.files_index)
    BridgeRequestHandler.files_range = bool(args.files_range)
    BridgeRequestHandler.dirs_map = {
        "3d": dir_3d,
        "mesh": dir_mesh,
        "video": dir_video,
    }

    # Create out_dir if missing
    os.makedirs(out_dir, exist_ok=True)

    # Start HTTP server thread
    http_thread = HttpServerThread(args.http_host, args.http_port, BridgeRequestHandler)
    http_thread.start()

    # Start watchers
    health_watcher = None
    flag_watcher = None
    if args.health_target:
        health_watcher = HealthWatcher(args.health_target, args.health_interval, args.health_threshold, tw)
        health_watcher.start()
    if args.ftps_enable and args.ftps_dir and args.ftps_restart_flag:
        flag_watcher = FtpsFlagWatcher(
            enabled=True,
            host=args.ftps_host.strip(),
            user=args.ftps_user.strip(),
            password=args.ftps_pass,
            remote_dir=args.ftps_dir.strip(),
            flag_name=args.ftps_restart_flag.strip(),
            interval=args.ftps_check_interval,
            tw=tw,
        )
        flag_watcher.start()

    def handle_signal(signum, frame):
        print("[main] shutdown requested", flush=True)
        try:
            if health_watcher:
                health_watcher.stop()
        except Exception:
            pass
        try:
            if flag_watcher:
                flag_watcher.stop()
        except Exception:
            pass
        try:
            tw.stop()
        except Exception:
            pass
        try:
            http_thread.stop()
        except Exception:
            pass

    try:
        signal.signal(signal.SIGINT, handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, handle_signal)
    except Exception:
        pass

    # Run writer loop (blocking)
    try:
        tw.run_forever()
    finally:
        try:
            if health_watcher:
                health_watcher.stop()
        except Exception:
            pass
        try:
            if flag_watcher:
                flag_watcher.stop()
        except Exception:
            pass
        try:
            tw.stop()
        except Exception:
            pass
        try:
            http_thread.stop()
        except Exception:
            pass
        print("[main] stopped", flush=True)

if __name__ == "__main__":
    main()
