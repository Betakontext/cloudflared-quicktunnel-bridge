### Cloudflared Quick Tunnel URL Bridge

## Overview

A small standard-library Python bridge for publishing and discovering Cloudflared Quick Tunnel URLs for local ComfyUI and, optionally, its output files. Cloudflared is required. Quick Tunnels have **no authentication**: anything exposed is public.

The supplied lifecycle senders upload FTPS flags using Python’s standard library. They do not run remote commands. Python **3.10+** is required by the supplied restart sender annotations.

## Workspace layout

```text
workspace/
├── ComfyUI/                         # main.py, .venv/, output/
└── cloudflared-quicktunnel-bridge/
    ├── cf_quicktunnel_writer.py      # bridge watcher
    ├── restart_bridge.py             # existing sender
    ├── restart_comfyui.py            # existing sender
    ├── stop_comfyui.py               # existing sender
    ├── stop_bridge.py                # new: root stop request only
    ├── cloudflared-windows-amd64.exe
    ├── .env
    ├── .env.files
    └── bridge_output/
```

Keep the bridge directory beside `ComfyUI` when using relative paths. An empty `CLOUDFLARED=` uses the executable beside the bridge script.

## Dotenv examples

Dotenv uses separate-line comments; no interpolation is performed. Existing process environment values win. For the existing senders, arguments are parsed **before** dotenv loading, so dotenv `FTPS_RETRIES` and `FTPS_TIMEOUT` do not change their argparse defaults: pass them explicitly on the command line. The new `stop_bridge.py` loads dotenv before resolving those two defaults.

### A — ComfyUI (`.env`)

```dotenv
CLOUDFLARED=
COMFY_URL=http://127.0.0.1:8188
HTTP_PORT=8799
OUT_DIR=bridge_output/comfy
HTTP_CORS=false
EDGE_PROTOCOL=http2
FILES_ENABLE=false
COMFY_ROOT=../ComfyUI
COMFY_RESTART_CMD=powershell.exe -NoProfile -File ".\restart_comfyui.ps1" -ForceStop -LowVRAM
COMFY_STOP_CMD=powershell.exe -NoProfile -File ".\stop_comfyui.ps1" -ForceStop
FTPS_HOST=ftp.example.com
FTPS_USER=your-user
FTPS_PASS=your-password
FTPS_DIR=/web-root/bridge
FTPS_RETRIES=5
```

### B — files (`.env.files`)

```dotenv
CLOUDFLARED=
COMFY_URL=http://127.0.0.1:8800
HTTP_PORT=8800
OUT_DIR=bridge_output/files
HTTP_CORS=false
EDGE_PROTOCOL=http2
FILES_ENABLE=true
FILES_ROOT=../ComfyUI/output
FILES_INDEX=true
FILES_RANGE=true
FTPS_HOST=ftp.example.com
FTPS_USER=your-user
FTPS_PASS=your-password
FTPS_DIR=/web-root/bridge/files
FTPS_RETRIES=5
```

`FILES_ROOT` is relative to the bridge directory. Example A tunnels ComfyUI at 8188; local bridge port 8799 is not tunneled through 8188.

## Start and reload

Use separate terminals for both modes. After changing dotenv, stop the bridge with Ctrl+C and relaunch it; root flags do not restart Python or reload dotenv.

```powershell
python cf_quicktunnel_writer.py --env-file .env
python cf_quicktunnel_writer.py --env-file .env.files
```

For portable lifecycle scripts, apply any path resolution change after their `param` block and ensure the bridge runs commands with bridge-directory cwd. This is a required implementation detail, not confirmation that such a patch is already present.

## Flag senders

```powershell
python restart_comfyui.py --env-file .env --message 'Restart ComfyUI'
python stop_comfyui.py --env-file .env --message 'Stop ComfyUI'
python restart_bridge.py --env-file .env --message 'Restart bridge'
python restart_bridge.py --env-file .env.files --message 'Restart files bridge'
python stop_bridge.py --env-file .env --message 'Stop tunnel request'
python stop_bridge.py --env-file .env.files --message 'Stop files tunnel request'
```

```bash
python3 restart_comfyui.py --env-file .env --message 'Restart ComfyUI'
python3 stop_comfyui.py --env-file .env --message 'Stop ComfyUI'
python3 restart_bridge.py --env-file .env --message 'Restart bridge'
python3 restart_bridge.py --env-file .env.files --message 'Restart files bridge'
python3 stop_bridge.py --env-file .env --message 'Stop tunnel request'
```

### Root stop is pending server support

**Do not treat `stop_bridge.py` as ready remote tunnel control.** It uploads only root `stop.flag` and requires a compatible new server-side root-stop handler. Such support is **not verified or established** by the provided watcher evidence/source. Request/provide the bridge source and handler design before using it operationally. No configuration names are assumed or invented here.

A root stop should be intended to stop the tunnel only while leaving Python alive, which requires the server implementation to suppress auto-restart/watchdog behavior, clear restart handling so a later start is possible, and invalidate/remove stale discovery on a true stop. Stopping ComfyUI does not stop its tunnel. A full Python-process restart is separate from flags.

## Flags and observed behavior

```text
bridge/
├── restart.flag                 # root action
├── stop.flag                    # pending root-stop handler only
├── comfy/
│   ├── restart.flag
│   └── stop.flag
└── files/
    └── restart.flag
```

Provided watcher evidence supports `comfy/restart.flag`, `comfy/stop.flag`, and root `restart.flag`; it does **not** establish root stop. Root restart is `cloudflared` or `both` according to the bridge’s existing configuration/implementation; do not infer a setting name. Logs showed `both` for A and `cloudflared` for B. Flags may be consumed before action success. Allow about 30 seconds and inspect logs. Upload success means only the request uploaded, never readiness or action success. `-ForceStop` interrupts jobs.

## Discovery and routes

Discovery paths are deployment-dependent placeholders, not invented source facts: consult the bridge implementation for actual paths. Typical documents contain a Quick Tunnel URL and update time; refresh after tunnel restart because URLs change. `/health` is bridge-only, not ComfyUI health. With files enabled, routes include images, 3d, mesh, and video; `?list=json` depends on `FILES_INDEX=true`. Range support depends on `FILES_RANGE=true`.

## FTPS, exits, and safety

FTPS is not SFTP. `stop_bridge.py` uses `ssl.create_default_context()` with `FTP_TLS(context=...)`, port 21, protected data connection, passive mode, an existing absolute `FTPS_DIR`, and certificate verification. The existing sender examples use FTPS flags, but their TLS verification behavior must be reviewed; do not assume it matches the new sender. The new sender rejects unsafe names/paths, retries with capped exponential backoff, and deletes its temporary flag automatically.

Uploader exits: `0` uploaded, `1` transfer/local error, `2` configuration error. Existing lifecycle scripts use their own semantics: `0` success, `1` failure, `2` already running. No sender can prove the requested lifecycle action happened.

## `.gitignore`

```gitignore
.env
.env.files
!.env.example
!.env.files.example
bridge_output/
__pycache__/
*.pyc
```

## License and contact

MIT License. Contact: [dev@betakontext.de](mailto:dev@betakontext.de) · <https://dev.betakontext.de> · <https://buymeacoffee.com/betakontext>
