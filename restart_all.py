#!/usr/bin/env python3
"""Send restart control requests to the bridge FTPS targets.

This program requests actions only; a successful upload is *not* execution
confirmation and it offers no receiver acknowledgement or ordering guarantee.
It never starts, stops, or otherwise runs a local/remote process, and never
creates remote directories.  Before storing each requested control flag it
lists that target and deletes only the exact opposite flag, preventing a stale
conflicting request.  Targets are attempted sequentially even when another
transfer fails; this is not an atomic transaction.

Upload order is ('comfy', 'files tunnel', 'main tunnel').  The order concerns uploads only, not execution.
"""
import argparse
import ftplib
import io
import json
import math
import os
import posixpath
import ssl
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ACTION = 'restart'
ORDER = ('comfy', 'files tunnel', 'main tunnel')

class ConfigError(ValueError):
    """Invalid local configuration; no network operation has been started."""

def read_dotenv(path):
    """Read simple UTF-8-sig KEY=VALUE dotenv data without expansion/execution."""
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ConfigError("cannot read dotenv file") from exc
    result = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            result[key] = value
    return result

def _find_auto(name, script_dir):
    for candidate in (script_dir / name, Path.cwd() / name):
        if candidate.is_file():
            return candidate
    return None

def _load(path, label):
    if path is None:
        return {}
    if not path.is_file():
        raise ConfigError("explicit %s file does not exist" % label)
    return read_dotenv(path)

def _unsafe(value):
    return not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value)

def _required(cfg, name):
    value = cfg.get(name)
    if _unsafe(value):
        raise ConfigError("missing or unsafe %s" % name)
    return value

def _directory(value, name):
    value = _required({name: value}, name)
    if not value.startswith("/") or "\\" in value or ".." in value.split("/"):
        raise ConfigError("%s must be an absolute safe POSIX directory" % name)
    return value.rstrip("/") or "/"

def _subdir(value):
    value = _required({"FTPS_COMFY_SUBDIR": value}, "FTPS_COMFY_SUBDIR")
    parts = PurePosixPath(value).parts
    if value.startswith("/") or "\\" in value or ".." in parts or value in (".", ""):
        raise ConfigError("FTPS_COMFY_SUBDIR must be a safe relative POSIX subdirectory")
    return value.strip("/")

def _flag(value, name):
    value = _required({name: value}, name)
    if "/" in value or "\\" in value or ".." in value:
        raise ConfigError("%s must be a plain flag name" % name)
    return value

def _positive(value, name, integer=False):
    # bool is deliberately rejected even though it is an int subclass.
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("%s must be positive" % name) from exc
    if isinstance(value, bool) or not math.isfinite(parsed) or parsed <= 0:
        raise ConfigError("%s must be positive" % name)
    if integer and (isinstance(value, float) or str(value).strip() != str(parsed)):
        raise ConfigError("%s must be a positive integer" % name)
    return parsed

def build_config(args):
    """Build isolated main/files snapshots: base then environment, overlay last."""
    script_dir = Path(__file__).resolve().parent
    base_path = Path(args.env_file) if args.env_file else (None if args.no_auto_env else _find_auto(".env", script_dir))
    files_path = Path(args.files_env) if args.files_env else (None if args.no_auto_env else _find_auto(".env.files", script_dir))
    base = _load(base_path, "base environment") if base_path else {}
    overlay = _load(files_path, "files environment") if files_path else {}
    main = dict(base)
    main.update(os.environ)              # existing process environment wins base
    files = dict(main)                   # retain an isolated main snapshot
    files.update(overlay)                # files overlay wins only for files target

    host = _required(main, "FTPS_HOST"); user = _required(main, "FTPS_USER"); password = _required(main, "FTPS_PASS")
    main_dir = _directory(main.get("FTPS_DIR"), "FTPS_DIR")
    comfy_dir = _subdir(main.get("FTPS_COMFY_SUBDIR", "comfy"))
    main_action = main.get("FTPS_FLAG_ACTION", "both")
    if main_action != "both":
        raise ConfigError("FTPS_FLAG_ACTION must be both for tunnel and Comfy requests")
    supplied_files_action = files.get("FTPS_FLAG_ACTION")
    if supplied_files_action is not None and supplied_files_action not in ("cloudflared", "both"):
        raise ConfigError("files FTPS_FLAG_ACTION must support cloudflared")

    # Priority: any merged FTPS_FILES_DIR, then an explicit overlay root different
    # from the isolated main root, otherwise the main root plus one /files.
    explicit_files = files.get("FTPS_FILES_DIR")
    if explicit_files:
        files_dir = _directory(explicit_files, "FTPS_FILES_DIR")
    elif overlay.get("FTPS_DIR") and _directory(overlay["FTPS_DIR"], "overlay FTPS_DIR") != main_dir:
        files_dir = _directory(overlay["FTPS_DIR"], "overlay FTPS_DIR")
    else:
        files_dir = _directory(main_dir.rstrip("/") + "/files", "files directory")
    fhost = _required(files, "FTPS_HOST"); fuser = _required(files, "FTPS_USER"); fpass = _required(files, "FTPS_PASS")
    retries = _positive(args.retries if args.retries is not None else main.get("FTPS_RETRIES", "3"), "retries", True)
    timeout = _positive(args.ftps_timeout if args.ftps_timeout is not None else main.get("FTPS_TIMEOUT", "30"), "FTPS timeout")

    tunnel_stop = _flag(main.get("FTPS_TUNNEL_STOP_FLAG", "stop.flag"), "FTPS_TUNNEL_STOP_FLAG")
    tunnel_restart = _flag(main.get("FTPS_RESTART_FLAG", "restart.flag"), "FTPS_RESTART_FLAG")
    files_stop = _flag(files.get("FTPS_TUNNEL_STOP_FLAG", "stop.flag"), "files FTPS_TUNNEL_STOP_FLAG")
    files_restart = _flag(files.get("FTPS_RESTART_FLAG", "restart.flag"), "files FTPS_RESTART_FLAG")
    comfy_stop = _flag(main.get("FTPS_STOP_FLAG_NAME", "stop.flag"), "FTPS_STOP_FLAG_NAME")
    comfy_restart = "restart.flag"
    if tunnel_stop == tunnel_restart or files_stop == files_restart or comfy_stop == comfy_restart:
        raise ConfigError("opposite control flag names must be distinct")
    desired_main, opposite_main = (tunnel_stop, tunnel_restart) if ACTION == "stop" else (tunnel_restart, tunnel_stop)
    desired_files, opposite_files = (files_stop, files_restart) if ACTION == "stop" else (files_restart, files_stop)
    desired_comfy, opposite_comfy = (comfy_stop, comfy_restart) if ACTION == "stop" else (comfy_restart, comfy_stop)
    targets = {
        "main tunnel": {"host":host,"user":user,"password":password,"directory":main_dir,"flag":desired_main,"opposite":opposite_main},
        "files tunnel": {"host":fhost,"user":fuser,"password":fpass,"directory":files_dir,"flag":desired_files,"opposite":opposite_files},
        "comfy": {"host":host,"user":user,"password":password,"directory":posixpath.join(main_dir, comfy_dir),"flag":desired_comfy,"opposite":opposite_comfy},
    }
    identities = [(t["host"], t["user"], t["directory"]) for t in targets.values()]
    if len(set(identities)) != len(identities):
        raise ConfigError("control targets must not collide")
    return targets, retries, timeout

def payload(message):
    """Return an in-memory JSON request; no local control files are made."""
    return json.dumps({"timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                       "action": ACTION, "request_id": str(uuid.uuid4()), "message": message},
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def _has_exact_name(client, name):
    """Use NLST and basename comparison; errors (including permission errors) propagate."""
    return name in {posixpath.basename(str(item).rstrip("/")) for item in client.nlst()}

def transfer(target, data, timeout):
    """Delete an existing exact opposite flag, then store the requested flag."""
    client = None
    try:
        client = ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=timeout)
        client.connect(target["host"], 21)
        client.login(target["user"], target["password"])
        client.prot_p()
        client.cwd(target["directory"])
        if _has_exact_name(client, target["opposite"]):
            client.delete(target["opposite"])
        client.storbinary("STOR " + target["flag"], io.BytesIO(data))
    finally:
        if client is not None:
            try: client.quit()
            except Exception:
                try: client.close()
                except Exception: pass

def attempt(label, target, data, retries, timeout):
    for number in range(1, retries + 1):
        try:
            transfer(target, data, timeout)
            print("uploaded %s: %s -> %s" % (label, target["directory"], target["flag"]))
            return True
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print("transfer failed %s attempt %d/%d: %s" % (label, number, retries, type(exc).__name__), file=sys.stderr)
            if number < retries:
                time.sleep(min(4.0, 0.25 * (2 ** (number - 1))))
    return False

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", help="explicit base dotenv path (missing is an error)")
    parser.add_argument("--files-env", help="explicit files dotenv overlay (missing is an error)")
    parser.add_argument("--no-auto-env", action="store_true", help="do not auto-discover .env/.env.files")
    parser.add_argument("--message", default="", help="optional JSON request message")
    parser.add_argument("--retries", type=int, help="positive attempts per target")
    parser.add_argument("--ftps-timeout", type=float, help="finite positive FTPS timeout seconds")
    parser.add_argument("--dry-run", action="store_true", help="validate and print targets; make no network connection")
    return parser.parse_args(argv)

def main(argv=None):
    try:
        args = parse_args(argv)
        targets, retries, timeout = build_config(args)  # validate every target before uploads
        labels = ORDER
        if args.dry_run:
            for label in labels:
                target = targets[label]
                print("dry-run %s: %s -> %s" % (label, target["directory"], target["flag"]))
            return 0
        data = payload(args.message)
        ok = True
        for label in labels:
            ok = attempt(label, targets[label], data, retries, timeout) and ok
        print("upload requests complete; this is not execution confirmation")
        return 0 if ok else 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr); return 1
    except ConfigError as exc:
        print("configuration error: %s" % exc, file=sys.stderr); return 2

if __name__ == "__main__":
    raise SystemExit(main())
