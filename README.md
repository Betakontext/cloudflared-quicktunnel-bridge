### Cloudflared Quick Tunnel URL Bridge

A small, standard-library Python bridge that publishes Cloudflared Quick Tunnel URLs for a local ComfyUI instance and, optionally, its output files.

Supports local and FTPS URL discovery, remote tunnel controls, separate ComfyUI lifecycle requests, and a browser-based file list with downloads and on-demand image previews.

### Important Security Notice

- Cloudflared is required.
- This bridge does not provide authentication for public tunnel routes.
- Treat published ComfyUI and file URLs as Internet-facing endpoints.
- FTPS is not SFTP: control requests use FTPS on port 21.
- Keep TLS certificate verification enabled and protect dotenv credentials.
- ComfyUI lifecycle commands are trusted local shell configuration.
- Stopping a tunnel or ComfyUI does not delete generated files.

### Requirements

- Python 3.10 or newer.
- A working `cloudflared` executable on the receiver.
- A running ComfyUI instance for the main tunnel.
- An FTPS account for remote controls and URL publication.
- Existing FTPS control directories with list, upload, and delete permissions.

No additional Python packages are required for the current file browser.

### Workspace Layout

Run the commands below from `bridge/`.

```text
workspace/
├── ComfyUI/
│   ├── main.py
│   ├── .venv/
│   └── output/
│       ├── 3d/
│       ├── mesh/
│       └── video/
└── bridge/
    ├── cf_quicktunnel_writer.py
    ├── bridge_control.py
    ├── stop_bridge.py
    ├── restart_bridge.py
    ├── stop_all.py
    ├── restart_all.py
    ├── stop_files.py
    ├── restart_files.py
    ├── stop_comfyui.py
    ├── restart_comfyui.py
    ├── stop_comfyui.ps1
    ├── restart_comfyui.ps1
    ├── cloudflared-windows-amd64.exe 
    ├── .env
    ├── .env.files
    └── bridge_output/
        ├── comfy/
        │   ├── tunnel_url.json
        │   └── tunnel_url.txt
        └── files/
            ├── tunnel_url.json
            └── tunnel_url.txt
```

### Merged Control Implementation

`tunnel_control.py` is no longer required. Do not install the old control patch in the merged writer.

`bridge_control.py` remains required by `stop_bridge.py` and `restart_bridge.py` on the sender machine.

The receiver does not import `bridge_control.py`.

### Cloudflared Executable

With the executable-discovery correction applied, an empty `CLOUDFLARED=` searches beside the script:

- Windows: `cloudflared-windows-amd64.exe`, then `cloudflared.exe`.
- Linux/macOS: `cloudflared`.
- Otherwise, falls back to `cloudflared` through `PATH`.

An explicitly configured executable overrides discovery.

Preset is in .env is an absolute path:

Get it f.e. from https://developers.cloudflare.com/tunnel/downloads/
and place it here in the Project root. 

```dotenv 
CLOUDFLARED=./cloudflared-windows-amd64.exe
```

### Main Receiver Profile: `.env`

Replace the FTPS credentials and verify the local PowerShell script arguments.

```dotenv
CLOUDFLARED=
COMFY_URL=http://127.0.0.1:8188
HTTP_HOST=127.0.0.1
HTTP_PORT=8799
OUT_DIR=bridge_output/comfy
HTTP_CORS=false
EDGE_PROTOCOL=http2
FILES_ENABLE=false

COMFY_RESTART_CMD=powershell.exe -NoProfile -File ".\restart_comfyui.ps1" -ForceStop -LowVRAM
COMFY_STOP_CMD=powershell.exe -NoProfile -File ".\stop_comfyui.ps1" -ForceStop

FTPS_ENABLE=true
FTPS_HOST=ftp.example.com
FTPS_USER=your-user
FTPS_PASS=your-password
FTPS_DIR=/web-root/bridge
FTPS_RETRIES=5
FTPS_TIMEOUT=30
FTPS_CHECK_INTERVAL=10
FTPS_RESTART_FLAG=restart.flag
FTPS_TUNNEL_STOP_FLAG=stop.flag
FTPS_FLAG_ACTION=both
FTPS_COMFY_SUBDIR=comfy
FTPS_STOP_FLAG_NAME=stop.flag
```

Set `FTPS_FLAG_ACTION=both` explicitly to enable tunnel and ComfyUI requests.

`HTTP_PORT=8799` is the bridge HTTP server. The main tunnel forwards to ComfyUI at `COMFY_URL`.

For Linux/macOS receivers, replace the PowerShell lifecycle commands with suitable local commands.

### Files Receiver Profile: `.env.files`

This is an independent receiver profile.

```dotenv
CLOUDFLARED=
COMFY_URL=http://127.0.0.1:8800
HTTP_HOST=127.0.0.1
HTTP_PORT=8800
OUT_DIR=bridge_output/files
HTTP_CORS=false
EDGE_PROTOCOL=http2

FILES_ENABLE=true
FILES_ROOT=../ComfyUI/output
FILES_IMAGES_DIR=../ComfyUI/output
FILES_3D_DIR=../ComfyUI/output/3d
FILES_MESH_DIR=../ComfyUI/output/mesh
FILES_VIDEO_DIR=../ComfyUI/output/video
FILES_INDEX=true
FILES_RANGE=true

FTPS_ENABLE=true
FTPS_HOST=ftp.example.com
FTPS_USER=your-user
FTPS_PASS=your-password
FTPS_DIR=/web-root/bridge/files
FTPS_FILES_DIR=/web-root/bridge/files
FTPS_RETRIES=5
FTPS_TIMEOUT=30
FTPS_CHECK_INTERVAL=10
FTPS_RESTART_FLAG=restart.flag
FTPS_TUNNEL_STOP_FLAG=stop.flag
FTPS_FLAG_ACTION=cloudflared
```

Adjust shared directories to match your setup.

`FTPS_FILES_DIR` is a sender setting; it does not replace the receiver's `FTPS_DIR`.

Keep ports, output directories, and FTPS directories distinct between profiles.

Sharing `ComfyUI/output` also exposes its reachable subdirectories. Use a dedicated images directory if necessary.

### Receiver Configuration Rules

With the profile-loader correction applied:

- `--env-file .env.files` loads only `.env.files`.
- Without `--env-file`, the receiver loads `.env` beside the script.
- Existing process environment values override dotenv values.
- Explicit command-line settings override corresponding environment settings.
- Relative receiver configuration paths are resolved against the script directory.
- Dotenv supports simple assignments, matching quotes, and separate-line comments.
- No variable interpolation or inline-comment removal is performed.

Restart the Python receiver after configuration changes. Tunnel restart flags do not reload dotenv.

Sender configuration precedence differs by helper; do not assume receiver rules apply to every sender.

### Start the Receiver Bridges

On Windows, use two separate terminals:

```powershell
python cf_quicktunnel_writer.py --env-file ".env"
```

```powershell
python cf_quicktunnel_writer.py --env-file ".env.files"
```

Keep both Python processes running for remote restart requests.

Stop pauses the tunnel, not the receiving Python process. The pause state is not persistent: restarting Python enables the tunnel again.

### File Browser

Open the current files Quick Tunnel URL to access the shared-folder overview.

| Route | Directory |
|---|---|
| `/files/images/` | `FILES_IMAGES_DIR`, falling back to `FILES_ROOT` |
| `/files/3d/` | `FILES_3D_DIR` |
| `/files/mesh/` | `FILES_MESH_DIR` |
| `/files/video/` | `FILES_VIDEO_DIR` |

With the latest browser updates:

- Files appear in a vertical list.
- Subdirectories are clickable.
- File names open the original files.
- Download links and Preview buttons use matching styling.
- Images load only after clicking Preview.
- Preview loads the full original image, not a reduced thumbnail.
- `?list=json` returns file names instead of the HTML listing.

`FILES_INDEX=true` enables directory listings. Disabling it does not prevent access to known file URLs.

The current implementation reads files fully into memory, including range requests. Large downloads may consume substantial memory.

### Discovery and Health

Each receiver writes `tunnel_url.json` and `tunnel_url.txt` to its `OUT_DIR` and optionally uploads them to its `FTPS_DIR`.

The JSON contains `url` and `updated_at`.

Local bridge endpoints:

```text
/health
/bridge/tunnel_url.json
/bridge/tunnel_url.txt
```

Quick Tunnel URLs may change after restart. Always use the current published URL.

Discovery clearing during pause or stop is best effort.

`/health` reports bridge state, not ComfyUI readiness or public tunnel reachability.

### Remote Control Mailbox

Create these directories before using remote controls:

```text
/web-root/bridge/
├── comfy/
└── files/
```

Senders upload temporary control flags:

```text
/web-root/bridge/
├── restart.flag
├── stop.flag
├── comfy/
│   ├── restart.flag
│   └── stop.flag
└── files/
    ├── restart.flag
    └── stop.flag
```

- Root flags control the main tunnel.
- `comfy/` flags control configured ComfyUI lifecycle commands.
- `files/` flags control the files tunnel.
- Stop takes precedence when conflicting flags are detected together.
- The examples configure polling every 10 seconds.
- Upload success does not confirm execution or readiness.
- Independent receivers do not guarantee execution order.

A terminated Python receiver cannot be restarted through flags. Use an OS service or autostart for process recovery.

### Sender Commands

Run from the directory containing the senders and their profiles.

These commands connect to the configured FTPS server, except `--dry-run`.

| Intent | Ubuntu | Windows |
|---|---|---|
| Stop everything | `python3 stop_all.py` | `python stop_all.py` |
| Restart everything | `python3 restart_all.py` | `python restart_all.py` |
| Validate stop targets | `python3 stop_all.py --dry-run` | `python stop_all.py --dry-run` |
| Validate restart targets | `python3 restart_all.py --dry-run` | `python restart_all.py --dry-run` |
| Stop both tunnels only | `python3 stop_bridge.py` | `python stop_bridge.py` |
| Restart both tunnels only | `python3 restart_bridge.py` | `python restart_bridge.py` |
| Stop main tunnel | `python3 stop_bridge.py --env-file .env` | `python stop_bridge.py --env-file .env` |
| Restart main tunnel | `python3 restart_bridge.py --env-file .env` | `python restart_bridge.py --env-file .env` |
| Stop files tunnel | `python3 stop_bridge.py --env-file .env.files` | `python stop_bridge.py --env-file .env.files` |
| Restart files tunnel | `python3 restart_bridge.py --env-file .env.files` | `python restart_bridge.py --env-file .env.files` |

The wrappers control tunnels only, never ComfyUI.

For dedicated ComfyUI or legacy files senders, inspect that version's `--help`. Prefer the wrappers above for files-tunnel control.

### All-Sender Options

| Option | Purpose |
|---|---|
| `--env-file PATH` | Main/base dotenv file |
| `--files-env PATH` | Files overlay dotenv file |
| `--no-auto-env` | Disable automatic dotenv discovery |
| `--message TEXT` | Optional request message |
| `--retries N` | Transfer attempts per target |
| `--ftps-timeout SECONDS` | FTPS timeout |
| `--dry-run` | Validate without connecting or uploading |

```bash
python3 stop_all.py --env-file .env --files-env .env.files --message "maintenance" --retries 5 --ftps-timeout 30
```

```bash
python3 restart_all.py --env-file .env --files-env .env.files --message "resume" --retries 5 --ftps-timeout 30
```

Stop upload order: files tunnel → main tunnel → ComfyUI.

Restart upload order: ComfyUI → files tunnel → main tunnel.

These are upload orders, not guaranteed execution orders.

### Local Checks — Windows

```powershell
Invoke-RestMethod "http://127.0.0.1:8799/health"
Invoke-RestMethod "http://127.0.0.1:8800/health"
Start-Process "http://127.0.0.1:8800/"
```

Display current public URLs:

```powershell
Get-Content ".\bridge_output\comfy\tunnel_url.txt"
Get-Content ".\bridge_output\files\tunnel_url.txt"
```

### Troubleshooting

- **Cloudflared not found:** Check executable discovery or set an absolute `CLOUDFLARED` path.
- **Files bridge uses port 8799:** Check the corrected profile loader and existing environment variables.
- **Only “Cloudflared URL bridge” appears:** Check `FILES_ENABLE=true`, the selected profile, and the file-browser update.
- **Directory appears as JSON:** Remove `?list=json` from the URL or navigation links.
- **Preview loads automatically:** Replace the old image block with the manual Preview implementation.
- **Flags are ignored:** Check receiver processes, FTPS directories, permissions, and `FTPS_FLAG_ACTION`.
- **Files target becomes `/files/files`:** Set `FTPS_FILES_DIR` explicitly for legacy files senders.
- **Upload succeeds but nothing happens:** Allow for polling and check receiver logs and lifecycle commands.
- **Public tunnel is unavailable:** Inspect cloudflared logs and use the latest published URL.
- **Browser changes are missing:** Restart the files receiver and reload the page.

### Git Hygiene

```gitignore
.env
.env.files
!.env.example
!.env.files.example
bridge_output/
__pycache__/
*.pyc
```

### License

Original MIT License.

### Contact

[dev@betakontext.de](mailto:dev@betakontext.de)

[dev.betakontext.de](https://dev.betakontext.de)

[Buy Me a Coffee](https://buymeacoffee.com/betakontext)
