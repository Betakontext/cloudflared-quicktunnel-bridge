#!/usr/bin/env python3
"""Upload a fixed ``stop.flag`` request to an existing FTPS files directory.

Configuration is read without executing dotenv values.  The base dotenv file is
loaded first, then existing process environment variables override its values,
and the files dotenv overlay overrides that merged configuration.  For the
remote directory, ``FTPS_FILES_DIR`` is used when set; otherwise it is exactly
``FTPS_DIR.rstrip('/') + '/files'``.  This deliberately does not try to detect
whether ``FTPS_DIR`` already names a files directory: set ``FTPS_FILES_DIR``
explicitly when that is desired.

A successful upload is only a request for a receiver to stop; it is not proof
that any process was stopped.  This program creates no directories, deletes no
remote files, and executes no remote commands.
"""
import argparse
import ftplib
import io
import math
import os
from pathlib import Path
import ssl
import sys
import time
from datetime import datetime, timezone


class ConfigError(ValueError):
    """Raised for invalid or incomplete local configuration."""


def read_dotenv(path):
    """Return simple KEY=VALUE entries from UTF-8 (with optional BOM) dotenv."""
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ConfigError("cannot read environment file %s: %s" % (path, exc)) from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def discover(filename, script_dir):
    """Find an automatic dotenv file beside this script, then in the CWD."""
    for candidate in (script_dir / filename, Path.cwd() / filename):
        if candidate.is_file():
            return candidate
    return None


def safe_value(name, value):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\x00"):
        raise ConfigError("missing or unsafe %s" % name)
    return value


def positive_number(name, value, integer=False):
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("%s must be a positive %s" % (name, "integer" if integer else "number")) from exc
    if (not isinstance(parsed, (int, float)) or not math.isfinite(parsed) or parsed <= 0
            or (integer and str(value).strip() != str(parsed))):
        raise ConfigError("%s must be a positive %s" % (name, "integer" if integer else "number"))
    return parsed


def build_config(args):
    """Load dotenv files and return validated connection settings."""
    script_dir = Path(__file__).resolve().parent
    base_path = Path(args.env_file) if args.env_file else None
    overlay_path = Path(args.files_env) if args.files_env else None
    base = {}
    overlay = {}
    if base_path is not None:
        if not base_path.is_file():
            raise ConfigError("explicit base environment file does not exist: %s" % base_path)
        base = read_dotenv(base_path)
    elif not args.no_auto_env:
        found = discover(".env", script_dir)
        if found:
            base = read_dotenv(found)
    if overlay_path is not None:
        if not overlay_path.is_file():
            raise ConfigError("explicit files environment file does not exist: %s" % overlay_path)
        overlay = read_dotenv(overlay_path)
    elif not args.no_auto_env:
        found = discover(".env.files", script_dir)
        if found:
            overlay = read_dotenv(found)

    # This order intentionally preserves the restart-files profile semantics.
    merged = dict(base)
    merged.update(os.environ)
    merged.update(overlay)
    host = safe_value("FTPS_HOST", merged.get("FTPS_HOST"))
    user = safe_value("FTPS_USER", merged.get("FTPS_USER"))
    password = safe_value("FTPS_PASS", merged.get("FTPS_PASS"))
    if merged.get("FTPS_FILES_DIR"):
        directory = safe_value("FTPS_FILES_DIR", merged["FTPS_FILES_DIR"])
    else:
        root = safe_value("FTPS_DIR", merged.get("FTPS_DIR"))
        directory = root.rstrip("/") + "/files"
    retries = args.retries if args.retries is not None else positive_number("FTPS_RETRIES", merged.get("FTPS_RETRIES", "3"), True)
    timeout = args.ftps_timeout if args.ftps_timeout is not None else positive_number("FTPS_TIMEOUT", merged.get("FTPS_TIMEOUT", "30"))
    retries = positive_number("retries", retries, True)
    timeout = positive_number("ftps timeout", timeout)
    return {"host": host, "user": user, "password": password, "directory": directory,
            "retries": retries, "timeout": timeout}


def request_payload(message):
    """Create the small human-readable request uploaded as stop.flag."""
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    text = "files stop request\nrequested_at_utc: %s\n" % stamp
    if message:
        text += "message: %s\n" % message
    return text.encode("utf-8")


def upload(config, message):
    """Perform one TLS-protected upload; FTP errors are allowed to propagate."""
    client = None
    try:
        client = ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=config["timeout"])
        client.connect(config["host"])
        client.login(config["user"], config["password"])
        client.prot_p()
        client.cwd(config["directory"])
        client.storbinary("STOR stop.flag", io.BytesIO(request_payload(message)))
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Upload an FTPS files stop request.")
    parser.add_argument("--env-file", help="explicit base dotenv file")
    parser.add_argument("--files-env", help="explicit files dotenv overlay")
    parser.add_argument("--no-auto-env", action="store_true", help="do not discover .env files")
    parser.add_argument("--message", default="", help="optional text placed in the request")
    parser.add_argument("--retries", type=int, help="positive number of upload attempts")
    parser.add_argument("--ftps-timeout", type=float, help="positive FTPS timeout in seconds")
    return parser.parse_args(argv)


def main(argv=None):
    """Run the CLI and return 0 on upload, 1 on upload failure, 2 on config error."""
    args = parse_args(argv)
    try:
        config = build_config(args)
    except ConfigError as exc:
        print("Configuration error: %s" % exc, file=sys.stderr)
        return 2
    print("FTPS target: %s/%s" % (config["directory"].rstrip("/"), "stop.flag"))
    last_error = None
    for attempt in range(config["retries"]):
        try:
            upload(config, args.message)
            print("Uploaded stop.flag request successfully. Upload is not execution confirmation.")
            return 0
        except Exception as exc:
            last_error = exc
            if attempt + 1 < config["retries"]:
                time.sleep(min(0.5 * (2 ** attempt), 8.0))
    print("FTPS upload failed after %d attempt(s): %s" % (config["retries"], last_error), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
