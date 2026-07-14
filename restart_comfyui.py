#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
restart_comfyui.py
Trigger a soft restart of a remote ComfyUI instance by uploading a 'restart.flag'
into the remote bridge directory dedicated to ComfyUI.

This script mirrors the behavior and conventions of restart_bridge.py/restart_files.py:
- Stdlib-only FTPS (ftplib.FTP_TLS)
- .env auto-discovery (next to script, then CWD) or explicit --env-file
- Exponential backoff retries
- Minimal, traceable file content with UTC timestamp
- Exit codes: 0 success, 1 upload failure, 2 configuration error

Environment (from .env and optionally overlays):
  Required:
    FTPS_HOST            FTPS server hostname
    FTPS_USER            FTPS username
    FTPS_PASS            FTPS password
    FTPS_DIR             Base remote directory (used for fallback)
  Optional:
    COMFY_FLAG_REMOTE_DIR  Explicit remote dir for Comfy flag (e.g., /bridge/comfy)
                           If not set, fallback is f"{FTPS_DIR.rstrip('/')}/comfy"
    FTPS_RETRIES           Default upload retries (default: 5)
    FTPS_TIMEOUT           FTPS connect timeout in seconds (default: 25)

CLI:
  --env-file         Path to .env (if omitted, auto-discover next to script, then CWD)
  --no-auto-env      Disable auto-discovery
  --flag-name        Override flag filename (default: 'restart.flag')
  --message          Optional note included in flag content
  --retries          Upload retries (default: env FTPS_RETRIES or 5)
  --ftps-timeout     FTPS timeout seconds (default: env FTPS_TIMEOUT or 25)

Typical use:
  python restart_comfyui.py --message "Update models" 
  -> uploads /bridge/comfy/restart.flag (or COMFY_FLAG_REMOTE_DIR/restart.flag)

Server-side expectation:
  Your cf_quicktunnel_writer.py (or a local watcher) polls COMFY_FLAG_WATCH_DIR for 'restart.flag'
  and runs a local restart command for ComfyUI upon detection.
"""

import argparse
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from ftplib import FTP_TLS
from contextlib import contextmanager

# ---------- .env loader (same semantics as bridge scripts) ----------

def _strip_quotes(value: str) -> str:
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value

def load_env_file(path: str, overwrite: bool = False) -> int:
    set_count = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = _strip_quotes(val.strip())
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

def auto_discover_env_file(script_dir: str, cwd: str) -> str | None:
    candidates = [os.path.join(script_dir, ".env")]
    if os.path.abspath(cwd) != os.path.abspath(script_dir):
        candidates.append(os.path.join(cwd, ".env"))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None

# ---------- helpers ----------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def atomic_write(path: str, data: bytes):
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

# ---------- FTPS helpers (stdlib) ----------

@contextmanager
def ftps_connect(host: str, user: str, password: str, timeout: float = 25.0):
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
    if not remote_dir or remote_dir == "/":
        return
    parts = [p for p in remote_dir.strip("/").split("/") if p]
    try:
        ftps.cwd("/")
    except Exception:
        pass
    path_so_far = ""
    for p in parts:
        path_so_far = path_so_far + "/" + p
        try:
            ftps.cwd(path_so_far)
        except Exception:
            try:
                ftps.mkd(path_so_far)
            except Exception as e:
                msg = str(e).lower()
                if "exists" not in msg and "file unavailable" not in msg:
                    raise
            ftps.cwd(path_so_far)

def ftps_upload_file(host: str, user: str, password: str, local_path: str, remote_dir: str, remote_filename: str, timeout: float = 25.0):
    with ftps_connect(host, user, password, timeout=timeout) as ftps:
        _ftps_mkdirs(ftps, remote_dir)
        if remote_dir and remote_dir != "/":
            ftps.cwd(remote_dir)
        with open(local_path, "rb") as lf:
            ftps.storbinary(f"STOR {remote_filename}", lf)

def upload_with_retries(host: str, user: str, password: str, local_path: str, remote_dir: str, remote_filename: str, retries: int = 5, base_delay: float = 1.0, timeout: float = 25.0) -> bool:
    attempt = 0
    last_exc: Exception | None = None
    while attempt < retries:
        try:
            ftps_upload_file(host, user, password, local_path, remote_dir, remote_filename, timeout=timeout)
            print(f"[ftps] uploaded -> {host}:{remote_dir}/{remote_filename}", flush=True)
            return True
        except Exception as e:
            last_exc = e
            delay = base_delay * (1.7 ** attempt)
            print(f"[ftps] upload failed (attempt {attempt+1}/{retries}): {e}; retrying in {delay:.1f}s", flush=True)
            time.sleep(delay)
            attempt += 1
    print(f"[ftps] upload permanently failed: {last_exc}", flush=True)
    return False

# ---------- main ----------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Trigger remote ComfyUI restart by uploading restart.flag to COMFY_FLAG_REMOTE_DIR (or FTPS_DIR/comfy)")
    p.add_argument("--env-file", type=str, default="", help="Path to .env file to load before execution")
    p.add_argument("--no-auto-env", action="store_true", help="Disable .env auto-discovery")
    p.add_argument("--flag-name", type=str, default="restart.flag", help="Flag filename to upload (default: restart.flag)")
    p.add_argument("--message", type=str, default="", help="Optional note included in flag content")
    p.add_argument("--retries", type=int, default=int(os.getenv("FTPS_RETRIES", "5")), help="Upload retries")
    p.add_argument("--ftps-timeout", type=float, default=float(os.getenv("FTPS_TIMEOUT", "25")), help="FTPS timeout seconds")
    return p.parse_args(argv)

def main():
    # Load .env
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cwd = os.getcwd()
    args = parse_args()

    if args.env_file:
        cnt = load_env_file(args.env_file, overwrite=False)
        print(f"[env] loaded {cnt} keys from {args.env_file}", flush=True)
    elif not args.no_auto_env:
        auto_env = auto_discover_env_file(script_dir, cwd)
        if auto_env:
            cnt = load_env_file(auto_env, overwrite=False)
            print(f"[env] auto-loaded {cnt} keys from {auto_env}", flush=True)
        else:
            print("[env] no .env discovered", flush=True)
    else:
        print("[env] auto-discovery disabled", flush=True)

    host = os.getenv("FTPS_HOST", "").strip()
    user = os.getenv("FTPS_USER", "").strip()
    pwd = os.getenv("FTPS_PASS", "")
    base_remote_dir = os.getenv("FTPS_DIR", "").strip()
    comfy_remote_dir = os.getenv("COMFY_FLAG_REMOTE_DIR", "").strip() or (f"{base_remote_dir.rstrip('/')}/comfy" if base_remote_dir else "")

    flag_name = (args.flag_name or "restart.flag").strip() or "restart.flag"

    # Validate
    missing = []
    if not host: missing.append("FTPS_HOST")
    if not user: missing.append("FTPS_USER")
    if not pwd: missing.append("FTPS_PASS")
    if not comfy_remote_dir: missing.append("COMFY_FLAG_REMOTE_DIR (or FTPS_DIR for fallback)")
    if missing:
        print(f"[main] config error: missing {', '.join(missing)}", flush=True)
        sys.exit(2)

    # Create local flag file content
    note = args.message.strip()
    content = f"comfyui restart requested at {utc_now_iso()}"
    if note:
        content += f"\nmessage: {note}"
    content_bytes = (content + "\n").encode("utf-8")

    # Write temp file
    tmp_dir = tempfile.gettempdir()
    local_flag_path = os.path.join(tmp_dir, f"{flag_name}.tmp-upload")
    try:
        atomic_write(local_flag_path, content_bytes)
    except Exception as e:
        print(f"[main] failed to prepare local flag: {e}", flush=True)
        sys.exit(1)

    print(f"[flag] uploading '{flag_name}' to {host}:{comfy_remote_dir}", flush=True)
    ok = upload_with_retries(
        host, user, pwd,
        local_flag_path, comfy_remote_dir, flag_name,
        retries=max(1, int(args.retries)),
        base_delay=1.0,
        timeout=float(args.ftps_timeout),
    )

    try:
        os.unlink(local_flag_path)
    except Exception:
        pass

    if ok:
        print("[flag] done", flush=True)
        sys.exit(0)
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
