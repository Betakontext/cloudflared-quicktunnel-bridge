#!/usr/bin/env python3
"""Upload a root stop.flag request via verified FTPS; it executes no remote command.

A compatible server-side root-stop handler is required. Uploading only records a
request and cannot confirm that a tunnel (or anything else) stopped.
"""
from __future__ import annotations

import argparse
import ftplib
import math
import os
from pathlib import Path
import ssl
import sys
import tempfile
import time
from datetime import datetime, timezone

DEFAULT_RETRIES = 5
DEFAULT_TIMEOUT = 30.0


class ConfigError(Exception):
    """A local configuration value is missing or unsafe."""


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines without overriding the existing environment."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError("Cannot read the dotenv file.") from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip())


def plain_flag_name(value: str) -> str:
    if (not value or value in {".", ".."} or "/" in value or "\\" in value
            or "\r" in value or "\n" in value):
        raise argparse.ArgumentTypeError("flag name must be a plain filename")
    return value


def positive_finite(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and positive")
    return number


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("retries must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("retries must be a positive integer")
    return number


def remote_directory(value: str) -> str:
    if not value or not value.startswith("/") or "\r" in value or "\n" in value or "\x00" in value:
        raise ConfigError("FTPS_DIR must be an absolute path without line breaks.")
    return value


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Missing required configuration: {name}.")
    return value


def upload_once(local_path: Path, host: str, user: str, password: str, directory: str,
                timeout: float, flag_name: str) -> None:
    client: ftplib.FTP_TLS | None = None
    try:
        context = ssl.create_default_context()
        client = ftplib.FTP_TLS(context=context, timeout=timeout)
        client.connect(host=host, port=21, timeout=timeout)
        client.login(user=user, passwd=password)
        client.prot_p()
        client.set_pasv(True)
        client.cwd(directory)  # The known bridge directory must already exist.
        with local_path.open("rb") as source:
            client.storbinary(f"STOR {flag_name}", source)
    finally:
        if client is not None:
            try:
                client.quit()
            except (OSError, EOFError, ftplib.Error):
                try:
                    client.close()
                except OSError:
                    pass


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Upload a root FTPS stop request; server-side execution is not confirmed."
    )
    p.add_argument("--env-file", metavar="PATH", help="dotenv file to load")
    p.add_argument("--no-auto-env", action="store_true", help="do not load .env automatically")
    p.add_argument("--flag-name", type=plain_flag_name, default="stop.flag")
    p.add_argument("--message", default="Stop bridge/tunnel request")
    p.add_argument("--retries", type=positive_int, default=None,
                   help="total upload attempts (default: FTPS_RETRIES or 5)")
    p.add_argument("--ftps-timeout", type=positive_finite, default=None,
                   help="per-operation seconds (default: FTPS_TIMEOUT or 30)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    dotenv = Path(args.env_file) if args.env_file else (None if args.no_auto_env else Path(".env"))
    if dotenv is not None:
        try:
            load_dotenv(dotenv)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
    try:
        retries = args.retries if args.retries is not None else positive_int(os.environ.get("FTPS_RETRIES", str(DEFAULT_RETRIES)))
        timeout = args.ftps_timeout if args.ftps_timeout is not None else positive_finite(os.environ.get("FTPS_TIMEOUT", str(DEFAULT_TIMEOUT)))
        host = required_env("FTPS_HOST")
        user = required_env("FTPS_USER")
        password = required_env("FTPS_PASS")
        directory = remote_directory(required_env("FTPS_DIR"))
    except (ConfigError, argparse.ArgumentTypeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # This text is informational only; no remote command is sent or encoded here.
    payload = f"requested_at_utc={timestamp}\nnote={args.message}\n"
    try:
        with tempfile.TemporaryDirectory(prefix="stop-bridge-") as temporary:
            local_flag = Path(temporary) / args.flag_name
            local_flag.write_text(payload, encoding="utf-8", newline="\n")
            for attempt in range(1, retries + 1):
                try:
                    upload_once(local_flag, host, user, password, directory, timeout, args.flag_name)
                    print("Request uploaded; execution is not confirmed.")
                    return 0
                except (OSError, EOFError, ftplib.Error):
                    if attempt < retries:
                        time.sleep(min(2 ** (attempt - 1), 30))
    except OSError:
        print("Transfer/local error: unable to prepare or upload the request.", file=sys.stderr)
        return 1
    print("Transfer/local error: upload did not complete.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
