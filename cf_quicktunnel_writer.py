#!/usr/bin/env python3
"""Cloudflared Quick Tunnel URL bridge (Python 3.10+; standard library only).

Relative local configuration paths, including an explicit --env-file, are resolved
against this script's directory rather than the current working directory.  The
HTTP server binds to 127.0.0.1 by default: public Quick Tunnels have no auth.
A pause is process memory only and is not persistent after Python restarts.
FTPS upload success acknowledges only upload, not remote control execution.
COMFY_RESTART_CMD and COMFY_STOP_CMD are trusted *local* shell configuration and
are deliberately run with shell=True; remote flag contents are never executed.
"""
from __future__ import annotations
import argparse, contextlib, ftplib, json, mimetypes, os, posixpath, queue, re, shlex
import signal, ssl, subprocess, sys, threading, time, urllib.parse, urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

SCRIPT_DIR = Path(__file__).resolve().parent
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com\b", re.I)

def local_path(value: str) -> Path:
    """Resolve a local configuration path against the program directory."""
    p = Path(value).expanduser()
    return (p if p.is_absolute() else SCRIPT_DIR / p).resolve()

def truth(value: str | bool | None, default: bool = False) -> bool:
    if value is None: return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

def simple_name(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and not any(c in value for c in "/\\\x00\r\n")

def safe_subdir(value: str) -> bool:
    p = PurePosixPath(value or "")
    return bool(value) and not p.is_absolute() and ".." not in p.parts and "\\" not in value and "\x00" not in value

def parse_dotenv(path: Path) -> dict[str, str]:
    """Small .env parser: quotes supported, no expansion and no inline comments."""
    result: dict[str, str] = {}
    try: lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError: return result
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"): continue
        if line.startswith("export "): line = line[7:].lstrip()
        if "=" not in line: continue
        key, value = line.split("=", 1); key = key.strip(); value = value.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'": value = value[1:-1]
            result[key] = value
    return result

def load_environment(env_file: str | None, no_auto: bool) -> None:
    """Load an explicit dotenv file, or auto-discover the default file."""
    if env_file:
        path = local_path(env_file)
        if not path.is_file():
            raise FileNotFoundError(f"Configuration file not found: {path}")
    elif not no_auto:
        path = SCRIPT_DIR / ".env"
        if not path.is_file():
            print(f"[env] No default configuration found: {path}", flush=True)
            return
    else:
        return

    values = parse_dotenv(path)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    print(f"[env] Loaded configuration: {path}", flush=True)


def env(name: str, default: str = "") -> str: return os.environ.get(name, default)
def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(data, encoding="utf-8"); os.replace(tmp, path)
    finally:
        try: tmp.unlink()
        except FileNotFoundError: pass

def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Return one inclusive byte range. Multiple/invalid ranges return None."""
    if not header.startswith("bytes=") or "," in header: return None
    spec = header[6:].strip()
    try:
        a, b = spec.split("-", 1)
        if not a:
            n = int(b); return (max(0, size - n), size - 1) if n > 0 else None
        start = int(a); end = int(b) if b else size - 1
        return (start, min(end, size - 1)) if 0 <= start <= end < size else None
    except (ValueError, OverflowError): return None

@contextlib.contextmanager
def ftps_connect(host: str, user: str, password: str, timeout: float) -> Iterator[ftplib.FTP_TLS]:
    """Verified TLS, port 21, protected passive FTP data; credentials never logged."""
    ftp: ftplib.FTP_TLS | None = None
    try:
        ftp = ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=timeout)
        ftp.connect(host, 21); ftp.login(user, password); ftp.prot_p(); ftp.set_pasv(True)
        yield ftp
    finally:
        if ftp:
            try: ftp.quit()
            except Exception:
                try: ftp.close()
                except Exception: pass

def default_cloudflared() -> str:
    """Prefer a bundled executable before falling back to PATH."""
    names = (
        ("cloudflared-windows-amd64.exe", "cloudflared.exe")
        if os.name == "nt"
        else ("cloudflared",)
    )
    for name in names:
        candidate = SCRIPT_DIR / name
        if candidate.is_file():
            return str(candidate)
    return "cloudflared"


@dataclass
class Config:
    cloudflared: str; comfy_url: str; out_dir: Path; protocol: str; http_host: str; http_port: int
    cors: bool; files_enable: bool; files_root: Path; files_3d_root: Path; files_mesh_root: Path; files_video_root: Path; files_index: bool; files_range: bool
    ftps_enable: bool; ftps_host: str; ftps_user: str; ftps_pass: str; ftps_dir: str; ftps_retries: int; ftps_timeout: float
    ftps_interval: float; ftps_restart_flag: str; ftps_tunnel_stop_flag: str; ftps_action: str; ftps_comfy_subdir: str; ftps_stop_flag: str
    comfy_watch_dir: Path | None; comfy_restart_cmd: str; comfy_stop_cmd: str; health_url: str; health_interval: float; health_threshold: int

    @classmethod
    def from_env(cls, a: argparse.Namespace) -> "Config":
        default_cf = default_cloudflared()
        root = local_path(a.files_root or env("FILES_ROOT", "../ComfyUI/output"))
        watch = a.comfy_flag_watch_dir or env("COMFY_FLAG_WATCH_DIR")
        return cls(
            a.cloudflared or env("CLOUDFLARED", default_cf) or default_cf, a.comfy_url or env("COMFY_URL", "http://127.0.0.1:8188"),
            local_path(a.out_dir or env("OUT_DIR", "bridge_output")), a.protocol or env("EDGE_PROTOCOL", env("PROTOCOL", "http2")),
            a.http_host or env("HTTP_HOST", "127.0.0.1"), int(a.http_port or env("HTTP_PORT", "8799")), truth(a.cors if a.cors is not None else env("HTTP_CORS")),
            truth(a.files_enable if a.files_enable is not None else env("FILES_ENABLE")), root, local_path(a.files_3d_root or env("FILES_3D_ROOT", env("FILES_3D_DIR", str(root / "3d")))), local_path(a.files_mesh_root or env("FILES_MESH_ROOT", env("FILES_MESH_DIR", str(root / "mesh")))), local_path(a.files_video_root or env("FILES_VIDEO_ROOT", env("FILES_VIDEO_DIR", str(root / "video")))), truth(a.files_index if a.files_index is not None else env("FILES_INDEX")), truth(a.files_range if a.files_range is not None else env("FILES_RANGE")),
            truth(a.ftps_enable if a.ftps_enable is not None else env("FTPS_ENABLE")), a.ftps_host or env("FTPS_HOST"), a.ftps_user or env("FTPS_USER"), a.ftps_pass or env("FTPS_PASS"), a.ftps_dir or env("FTPS_DIR"),
            int(a.ftps_retries or env("FTPS_RETRIES", "3")), float(a.ftps_timeout or env("FTPS_TIMEOUT", "25")), float(a.ftps_check_interval or env("FTPS_CHECK_INTERVAL", "10")),
            a.ftps_restart_flag or env("FTPS_RESTART_FLAG", "restart.flag"), a.ftps_tunnel_stop_flag or env("FTPS_TUNNEL_STOP_FLAG", "stop.flag"), a.ftps_flag_action or env("FTPS_FLAG_ACTION", "cloudflared"),
            a.ftps_comfy_subdir or env("FTPS_COMFY_SUBDIR", "comfy"), a.ftps_stop_flag_name or env("FTPS_STOP_FLAG_NAME", "stop.flag"),
            local_path(watch) if watch else None, a.comfy_restart_cmd or env("COMFY_RESTART_CMD"), a.comfy_stop_cmd or env("COMFY_STOP_CMD"),
            a.health_url or env("HEALTH_URL", ""), float(a.health_check_interval or env("HEALTH_CHECK_INTERVAL", "30")), int(a.health_failure_threshold or env("HEALTH_FAILURE_THRESHOLD", "3")))

class TunnelWriter:
    def __init__(self, cfg: Config):
        self.cfg, self.state_lock, self.action_lock = cfg, threading.Condition(threading.RLock()), threading.Lock()
        self.desired_enabled, self.generation, self.current_url, self.updated_at = True, 0, "", ""
        self.proc: subprocess.Popen[str] | None = None; self.stop_event = threading.Event(); self.log_queue: queue.Queue[str] = queue.Queue()
        self.status = "starting"; self.empty_sent = False
    def is_paused(self) -> bool:
        with self.state_lock: return not self.desired_enabled
    def is_running(self) -> bool:
        with self.state_lock: return bool(self.proc and self.proc.poll() is None)
    def snapshot(self) -> dict[str, Any]:
        with self.state_lock: return {"cloudflared_running": bool(self.proc and self.proc.poll() is None), "paused": not self.desired_enabled, "url": self.current_url, "updated_at": self.updated_at, "status": self.status}
    def request_pause(self) -> None:
        with self.state_lock: self.desired_enabled = False; self.generation += 1; self.status = "paused"; self.state_lock.notify_all()
        print("Tunnel action: pause requested", flush=True)
    def request_restart(self, manual: bool = False) -> bool:
        with self.state_lock:
            if not self.desired_enabled and not manual: return False
            if manual: self.desired_enabled = True
            self.generation += 1; self.status = "restart requested"; self.state_lock.notify_all()
        print("Tunnel action: manual restart requested" if manual else "Tunnel action: restart requested", flush=True); return True
    def stop(self) -> None:
        self.stop_event.set()
        with self.state_lock: self.desired_enabled = False; self.generation += 1; self.status = "stopping"; self.state_lock.notify_all()
    def write_local(self, url: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        atomic_write(self.cfg.out_dir / "tunnel_url.json", json.dumps({"url": url, "updated_at": now}, indent=2) + "\n")
        atomic_write(self.cfg.out_dir / "tunnel_url.txt", url + "\n")
    def _mkdirs(self, ftp: ftplib.FTP_TLS, directory: str) -> None:
        current = "" if directory.startswith("/") else "."
        for part in PurePosixPath(directory).parts:
            if part == "/": current = "/"; continue
            current = posixpath.join(current, part)
            try: ftp.mkd(current)
            except ftplib.error_perm: pass
    def publish_remote(self, url: str) -> None:
        c = self.cfg
        if not (c.ftps_enable and all((c.ftps_host,c.ftps_user,c.ftps_pass,c.ftps_dir))): return
        payloads = {"tunnel_url.txt": (url + "\n").encode(), "tunnel_url.json": (json.dumps({"url":url,"updated_at":self.updated_at}) + "\n").encode()}
        for attempt in range(max(1, c.ftps_retries)):
            try:
                with ftps_connect(c.ftps_host,c.ftps_user,c.ftps_pass,c.ftps_timeout) as ftp:
                    self._mkdirs(ftp,c.ftps_dir); ftp.cwd(c.ftps_dir)
                    import io
                    for name, data in payloads.items(): ftp.storbinary("STOR " + name, io.BytesIO(data))
                return
            except Exception as exc:
                print(f"FTPS publication attempt {attempt + 1} failed: {type(exc).__name__}", flush=True)
                if attempt + 1 < max(1,c.ftps_retries): time.sleep(min(8, 2 ** attempt))
    def invalidate(self) -> None:
        with self.state_lock:
            if self.empty_sent: return
            self.current_url = ""; self.updated_at = datetime.now(timezone.utc).isoformat(); self.empty_sent = True
        self.write_local(""); self.publish_remote("")
    def _terminate(self, p: subprocess.Popen[str]) -> bool:
        if p.poll() is not None: return True
        try: p.terminate(); p.wait(timeout=3)
        except Exception:
            try: p.kill(); p.wait(timeout=3)
            except Exception: print("cloudflared did not terminate; refusing duplicate spawn", flush=True); return False
        return True
    def _reader(self, p: subprocess.Popen[str]) -> None:
        if p.stdout:
            for line in iter(p.stdout.readline, ""): self.log_queue.put(line)
    def run_forever(self) -> None:
        backoff = 2.0
        while not self.stop_event.is_set():
            with self.state_lock: enabled, generation = self.desired_enabled, self.generation
            if not enabled:
                self.invalidate()
                with self.state_lock: self.state_lock.wait(0.5)
                continue
            try:
                p = subprocess.Popen([self.cfg.cloudflared,"tunnel","--no-autoupdate","--protocol",self.cfg.protocol,"--url",self.cfg.comfy_url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            except Exception as exc:
                print(f"cloudflared spawn failed: {type(exc).__name__}", flush=True); time.sleep(backoff); backoff=min(30,backoff*2); continue
            with self.state_lock: self.proc=p; self.status="running"
            print("cloudflared process started", flush=True); threading.Thread(target=self._reader,args=(p,),daemon=True).start(); changed=False
            while not self.stop_event.is_set() and p.poll() is None:
                with self.state_lock: changed = (not self.desired_enabled or generation != self.generation)
                if changed: break
                try: line=self.log_queue.get(timeout=.25)
                except queue.Empty: continue
                print("cloudflared: " + line.rstrip(), flush=True)
                m=URL_RE.search(line)
                if m:
                    with self.state_lock: valid=self.desired_enabled and generation == self.generation
                    if valid:
                        url=m.group(0)
                        with self.state_lock: self.current_url=url; self.updated_at=datetime.now(timezone.utc).isoformat(); self.empty_sent=False
                        self.write_local(url)
                        with self.state_lock: valid=self.desired_enabled and generation == self.generation
                        if valid: self.publish_remote(url)
            # Actual child exits before potentially slow invalidation publication.
            if not self._terminate(p): return
            with self.state_lock: self.proc=None
            self.invalidate()
            if not self.stop_event.is_set() and not changed: time.sleep(backoff); backoff=min(30,backoff*2)
            else: backoff=2
        self.invalidate(); print("cloudflared process loop stopped", flush=True)

class FlagWatcher:
    def __init__(self, tw: TunnelWriter): self.tw=tw; self.stop_event=threading.Event()
    def _names(self, ftp: ftplib.FTP_TLS, path: str) -> set[str]:
        try: return {posixpath.basename(x.rstrip("/")) for x in ftp.nlst(path)}
        except Exception: return set()
    def _consume(self, ftp: ftplib.FTP_TLS, path: str, name: str) -> bool:
        try: ftp.delete(posixpath.join(path,name)); return True
        except Exception: print(f"FTPS control flag consumption failed: {name}", flush=True); return False
    def _comfy(self, restart: bool) -> None:
        cmd=self.tw.cfg.comfy_restart_cmd if restart else self.tw.cfg.comfy_stop_cmd
        if not cmd: return
        with self.tw.action_lock:
            print("Comfy action: " + ("restart" if restart else "stop"),flush=True)
            try: subprocess.run(cmd,shell=True,cwd=SCRIPT_DIR,check=False)
            except Exception as exc: print(f"Comfy command failed: {type(exc).__name__}",flush=True)
    def cycle(self) -> None:
        c=self.tw.cfg
        valid= c.ftps_enable and all((c.ftps_host,c.ftps_user,c.ftps_pass,c.ftps_dir))
        if not valid: return
        try:
            with ftps_connect(c.ftps_host,c.ftps_user,c.ftps_pass,c.ftps_timeout) as ftp:
                root=self._names(ftp,c.ftps_dir)
                if c.ftps_action in ("cloudflared","both") and simple_name(c.ftps_tunnel_stop_flag):
                    stop=c.ftps_tunnel_stop_flag in root; restart=simple_name(c.ftps_restart_flag) and c.ftps_restart_flag in root
                    if stop:
                        if restart and c.ftps_restart_flag != c.ftps_tunnel_stop_flag: self._consume(ftp,c.ftps_dir,c.ftps_restart_flag)
                        self._consume(ftp,c.ftps_dir,c.ftps_tunnel_stop_flag); self.tw.request_pause() # pause even delete fails
                    elif restart and self._consume(ftp,c.ftps_dir,c.ftps_restart_flag): self.tw.request_restart(manual=True)
                if c.ftps_action in ("comfy","both") and safe_subdir(c.ftps_comfy_subdir):
                    path=posixpath.join(c.ftps_dir,c.ftps_comfy_subdir); names=self._names(ftp,path); stop=c.ftps_stop_flag in names; restart="restart.flag" in names
                    if stop:
                        if restart and c.ftps_stop_flag != "restart.flag": self._consume(ftp,path,"restart.flag")
                        if self._consume(ftp,path,c.ftps_stop_flag): self._comfy(False)
                    elif restart and self._consume(ftp,path,"restart.flag"): self._comfy(True)
        except Exception as exc: print(f"FTPS flag poll failed: {type(exc).__name__}",flush=True)
    def run(self) -> None:
        while not self.stop_event.wait(self.tw.cfg.ftps_interval): self.cycle()

class LocalComfyWatcher:
    def __init__(self, tw: TunnelWriter): self.tw=tw; self.stop_event=threading.Event()
    def run(self) -> None:
        c=self.tw.cfg; d=c.comfy_watch_dir
        if not d: return
        while not self.stop_event.wait(c.ftps_interval):
            stop=d / c.ftps_stop_flag; restart=d / "restart.flag"
            if stop.is_file():
                try: stop.unlink(); restart.unlink(missing_ok=True); FlagWatcher(self.tw)._comfy(False)
                except OSError: pass
            elif restart.is_file():
                try: restart.unlink(); FlagWatcher(self.tw)._comfy(True)
                except OSError: pass

class HealthChecker:
    def __init__(self, tw: TunnelWriter): self.tw=tw; self.stop_event=threading.Event()
    def run(self) -> None:
        c=self.tw.cfg; failures=0
        if not c.health_url: return
        while not self.stop_event.wait(c.health_interval):
            try:
                with urllib.request.urlopen(c.health_url,timeout=5) as r:
                    if not 200 <= r.status < 400: raise OSError("bad HTTP status")
                failures=0
            except Exception:
                failures += 1
                if failures >= c.health_threshold: self.tw.request_restart(False); failures=0

class BridgeHandler(BaseHTTPRequestHandler):
    server: "BridgeServer"
    def log_message(self, fmt: str, *args: Any) -> None: print("HTTP: " + fmt % args, flush=True)
    def _send(self, status: int, body: bytes=b"", content_type: str="text/plain; charset=utf-8", extra: dict[str,str]|None=None) -> None:
        self.send_response(status); self.send_header("Content-Type",content_type); self.send_header("Content-Length",str(len(body)))
        if self.server.cfg.cors: self.send_header("Access-Control-Allow-Origin","*")
        for k,v in (extra or {}).items(): self.send_header(k,v)
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)
    def do_OPTIONS(self) -> None: self._send(204,b"",extra={"Access-Control-Allow-Methods":"GET, HEAD, OPTIONS"})
    def do_HEAD(self) -> None: self.do_GET()
    def do_GET(self) -> None:
        u=urllib.parse.urlsplit(self.path); path=urllib.parse.unquote(u.path)
        if path == "/":
            if not self.server.cfg.files_enable:
                return self._send(200, b"Cloudflared URL bridge\n")
            body = (
                "<!doctype html><html lang='de'><meta charset='utf-8'>"
                "<title>Shared folders in comfyui/output</title>"
                "<h1>Shared folders in comfyui/output</h1><ul>"
                "<li><a href='/files/images/'>Bilder</a></li>"
                "<li><a href='/files/3d/'>3D</a></li>"
                "<li><a href='/files/mesh/'>Mesh</a></li>"
                "<li><a href='/files/video/?'>Videos</a></li>"
                "</ul><p></p></html>"
            )
            return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

        if path == "/health": return self._send(200,json.dumps(self.server.writer.snapshot()).encode(),"application/json")
        if path in ("/bridge/tunnel_url.json","/bridge/tunnel_url.txt"):
            f=self.server.cfg.out_dir / path.rsplit("/",1)[1]
            return self._send(200,f.read_bytes() if f.is_file() else (b"{}\n" if path.endswith("json") else b"\n"),"application/json" if path.endswith("json") else "text/plain")
        if not self.server.cfg.files_enable: return self._send(404,b"Not found\n")
        parts=path.strip("/").split("/",1)
        roots = {
            "images": local_path(env("FILES_IMAGES_DIR") or str(self.server.cfg.files_root)),
            "3d": self.server.cfg.files_3d_root,
            "mesh": self.server.cfg.files_mesh_root,
            "video": self.server.cfg.files_video_root,
        }
        route = path.strip("/")
        if route.startswith("files/"):
            route = route[len("files/"):]
        parts = route.split("/", 1)
        if parts[0] not in roots:
            return self._send(404, b"Not found\n")
        base = roots[parts[0]]
        try:
            target=(base / (parts[1] if len(parts)>1 else "")).resolve(); target.relative_to(base.resolve())
        except (ValueError, OSError): return self._send(403,b"Forbidden\n")
        if target.is_dir():
            from html import escape

            if not self.server.cfg.files_index:
                return self._send(403, b"Directory listing disabled\n")

            try:
                entries = []
                for entry in target.iterdir():
                    try:
                        # Never list symbolic links pointing outside the shared root.
                        entry.resolve().relative_to(base.resolve())
                        if entry.is_dir() or entry.is_file():
                            entries.append(entry)
                    except (OSError, ValueError):
                        continue
                entries.sort(key=lambda entry: (not entry.is_dir(), entry.name.casefold()))
            except OSError:
                return self._send(403, b"Directory cannot be read\n")

            query = urllib.parse.parse_qs(u.query)
            if query.get("list") == ["json"]:
                names = [entry.name for entry in entries if entry.is_file()]
                return self._send(
                    200,
                    json.dumps(names, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )

            # Quote each URL component independently of HTML escaping.
            directory_url = urllib.parse.quote(path.rstrip("/") + "/", safe="/")
            cards = []
            preview_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp"}

            for entry in entries:
                name = escape(entry.name)
                url = directory_url + urllib.parse.quote(entry.name, safe="")

                if entry.is_dir():
                    cards.append(
                        f'<article><a href="{escape(url + "/", quote=True)}">'
                        f"📁 {name}/</a></article>"
                    )
                    continue

                href = escape(url, quote=True)
                preview = ""

                if entry.suffix.lower() in preview_extensions:
                    # Create the image only after the user requests a preview.
                    preview = (
                        f'<button type="button" data-src="{href}" '
                        'onclick="'
                        "const image = document.createElement('img');"
                        "image.alt = 'Bildvorschau';"
                        "image.src = this.dataset.src;"
                        "this.parentElement.appendChild(image);"
                        "this.disabled = true;"
                        "this.textContent = 'Preview geladen';"
                        '">Preview</button>'
                    )

                cards.append(
                    "<article>"
                    f'<a class="filename" href="{href}">{name}</a>'
                    '<span class="actions">'
                    f'<a href="{href}" download>Herunterladen</a>'
                    f"{preview}"
                    "</span>"
                    "</article>"
                )


            title = escape(path)
            body = (
                "<!doctype html><html lang='de'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                f"<title>{title}</title>"
                "<style>"
                "body{font-family:system-ui,sans-serif;margin:2rem;"
                "background:#f4f5f7;color:#222}"
                "main{display:flex;flex-direction:column;gap:.5rem;"
                "max-width:1100px}"
                "article{display:flex;flex-wrap:wrap;align-items:center;"
                "gap:1rem;background:white;padding:1rem;border-radius:8px;"
                "overflow-wrap:anywhere}"
                ".filename{flex:1 1 260px;min-width:0}"
                ".actions{display:flex;flex-wrap:wrap;gap:.5rem;"
                "align-items:center}"
                ".actions a,button{display:inline-block;box-sizing:border-box;"
                "font:inherit;font-size:.9rem;line-height:1.3;"
                "padding:.4rem .8rem;border:1px solid #b8c2ce;"
                "border-radius:6px;background:#eef2f6;color:#223;"
                "text-decoration:none;cursor:pointer}"
                ".actions a:hover,button:not(:disabled):hover{background:#dfe7ef}"
                "button:disabled{opacity:.6;cursor:default}"
                "img{display:block;flex-basis:100%;min-width:0;"
                "max-width:100%;max-height:400px;object-fit:contain;"
                "object-position:left center}"
                "a{color:#1458ad}"
                "</style></head><body>"

                f"<h1>{title}</h1>"
                "<p><a href='/'>← Ordnerübersicht</a></p>"
                f"<main>{''.join(cards) or '<p>Dieser Ordner ist leer.</p>'}</main>"
                "</body></html>"
            )
            return self._send(
                200,
                body.encode("utf-8"),
                "text/html; charset=utf-8",
                {"Cache-Control": "no-store"},
            )

        if not target.is_file(): return self._send(404,b"Not found\n")
        size=target.stat().st_size; rng=parse_range(self.headers.get("Range",""),size) if self.server.cfg.files_range else None
        if self.headers.get("Range") and self.server.cfg.files_range and rng is None: return self._send(416,b"",extra={"Content-Range":f"bytes */{size}"})
        start,end=rng if rng else (0,size-1); data=target.read_bytes()[start:end+1]
        extra={"Accept-Ranges":"bytes"}
        if rng: extra["Content-Range"]=f"bytes {start}-{end}/{size}"
        return self._send(206 if rng else 200,data,mimetypes.guess_type(str(target))[0] or "application/octet-stream",extra)

class BridgeServer(ThreadingHTTPServer):
    def __init__(self, cfg: Config, writer: TunnelWriter): super().__init__((cfg.http_host,cfg.http_port),BridgeHandler); self.cfg,self.writer=cfg,writer

def parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(description="Cloudflared Quick Tunnel URL bridge")
    p.add_argument("--env-file"); p.add_argument("--no-auto-env",action="store_true")
    for n in ("cloudflared","comfy-url","out-dir","protocol","http-host","http-port","files-root","files-3d-root","files-mesh-root","files-video-root","ftps-host","ftps-user","ftps-pass","ftps-dir","ftps-retries","ftps-timeout","ftps-check-interval","ftps-restart-flag","ftps-tunnel-stop-flag","ftps-flag-action","ftps-comfy-subdir","ftps-stop-flag-name","comfy-flag-watch-dir","comfy-restart-cmd","comfy-stop-cmd","health-url","health-check-interval","health-failure-threshold"): p.add_argument("--"+n)
    for n in ("cors","files-enable","files-index","files-range","ftps-enable"): p.add_argument("--"+n,dest=n.replace("-","_"),action=argparse.BooleanOptionalAction,default=None)
    return p
def main(argv: list[str] | None=None) -> int:
    # Stage one only identifies dotenv policy; process env has priority over it.
    early=argparse.ArgumentParser(add_help=False); early.add_argument("--env-file"); early.add_argument("--no-auto-env",action="store_true")
    first,_=early.parse_known_args(argv); load_environment(first.env_file,first.no_auto_env); args=parser().parse_args(argv); cfg=Config.from_env(args)
    writer=TunnelWriter(cfg); server=BridgeServer(cfg,writer); watcher=FlagWatcher(writer); comfy=LocalComfyWatcher(writer); health=HealthChecker(writer)
    threads=[threading.Thread(target=writer.run_forever,daemon=True),threading.Thread(target=watcher.run,daemon=True),threading.Thread(target=comfy.run,daemon=True),threading.Thread(target=health.run,daemon=True)]
    for t in threads:t.start()
    def shutdown(signum: int, frame: Any) -> None:
        print("Signal received; graceful shutdown requested",flush=True); writer.stop(); watcher.stop_event.set(); comfy.stop_event.set(); health.stop_event.set(); threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGINT,shutdown); signal.signal(signal.SIGTERM,shutdown)
    print(f"Bridge listening at http://{cfg.http_host}:{cfg.http_port}",flush=True)
    try: server.serve_forever()
    finally: shutdown(0,None); server.server_close()
    return 0
if __name__ == "__main__": raise SystemExit(main())
