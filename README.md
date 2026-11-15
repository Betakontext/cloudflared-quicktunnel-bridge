### Cloudflared Quick Tunnel URL Bridge

A small, standard-library Python bridge that publishes Cloudflared Quick Tunnel URLs for a local ComfyUI instance and, optionally, its output files.

It can publish discovery data locally and through FTPS, and can poll an FTPS mailbox for tunnel and Comfy lifecycle requests.

This README is a complete replacement guide for the current bridge helpers, control patch, and senders.

### Important security notice

Cloudflared is required.

Quick Tunnels have no built-in authentication.

An exposed route is public unless you protect it separately.

Treat published ComfyUI and file URLs as Internet-facing endpoints.

FTPS is not SFTP: the senders use FTPS on port 21.

The inspected newer sources use Python's default verified TLS certificate context.

Do not disable certificate verification.

Protect the FTP account and dotenv credentials as secrets.

Stopping a tunnel or ComfyUI does not delete generated data.

### Requirements

* Python 3.10 or newer is required for the supplied `restart_files.py` annotation syntax.
* A working `cloudflared` executable is required on the receiver machine.
* ComfyUI and this bridge are local sibling directories.
* An FTPS account is optional for URL publication and remote controls, but required for those features.
* Existing remote directories must be listable/writable/deletable by that FTPS account.

### Workspace layout

Keep the directories as siblings. Run bridge commands from the bridge directory.

```text
workspace/
├── ComfyUI/
│   ├── main.py
│   ├── .venv/
│   └── output/
└── bridge/
    ├── cf_quicktunnel_writer.py
    ├── tunnel_control.py
    ├── bridge_control.py
    ├── stop_bridge.py
    ├── restart_bridge.py
    ├── restart_files.py
    ├── stop_files.py
    ├── restart_comfyui.py
    ├── stop_comfyui.py
    ├── stop_all.py
    ├── restart_all.py
    ├── restart_comfyui.ps1
    ├── stop_comfyui.ps1
    ├── cloudflared-windows-amd64.exe
    ├── .env
    ├── .env.files
    └── bridge_output/
```

Relative paths below are resolved from `bridge/`.

An empty `CLOUDFLARED=` means the original setup uses the executable beside the bridge directory.

Do not assume platform executable autodetection.

### Install the corrected control patch

Apply the corrected `tunnel_control.py` integration to the writer source.

Classes must already be defined before installing the patch.

Immediately before the existing main guard, add:

```python
from tunnel_control import install
install(globals())

if __name__ == '__main__':
    main()
```

The installation is additive and does not itself start a service.

The corrected patch prevents repeated Comfy restarts; install it rather than relying on an older pasted variant.

### Main receiver profile: `.env`

Create this file in `bridge/` and replace the example FTPS credentials.

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
FTPS_ENABLE=true
FTPS_HOST=ftp.example.com
FTPS_USER=your-user
FTPS_PASS=your-password
FTPS_DIR=/web-root/bridge
FTPS_RETRIES=5
FTPS_TIMEOUT=30
FTPS_RESTART_FLAG=restart.flag
FTPS_TUNNEL_STOP_FLAG=stop.flag
FTPS_FLAG_ACTION=both
FTPS_COMFY_SUBDIR=comfy
FTPS_STOP_FLAG_NAME=stop.flag
```

`FTPS_FLAG_ACTION=both` is essential for the all-sender main profile; it is the default when absent in those sources, but set it explicitly.

`HTTP_PORT=8799` is the bridge HTTP server, not a tunnel through ComfyUI at port 8188.

### Files receiver profile: `.env.files`

This is a standalone receiver profile for the optional output-file bridge.

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
FTPS_ENABLE=true
FTPS_HOST=ftp.example.com
FTPS_USER=your-user
FTPS_PASS=your-password
FTPS_DIR=/web-root/bridge/files
FTPS_FILES_DIR=/web-root/bridge/files
FTPS_RETRIES=5
FTPS_TIMEOUT=30
FTPS_RESTART_FLAG=restart.flag
FTPS_TUNNEL_STOP_FLAG=stop.flag
FTPS_FLAG_ACTION=cloudflared
```

`FILES_ROOT` is relative to the bridge directory.

`FTPS_FILES_DIR` is an explicit sender target for dedicated files senders.

It is a sender hint and does not replace the receiver's `FTPS_DIR`.

Keep `OUT_DIR`, HTTP ports, and FTPS directories distinct between profiles.

### Dotenv rules and precedence

Dotenv parsing supports separate-line comments and simple `KEY=VALUE` entries.

Quoted values are unquoted; no interpolation or command execution is performed.

Restart the relevant Python bridge after a dotenv change. FTPS flags do not reload dotenv.

For `stop_all.py` and `restart_all.py`, the main configuration is base dotenv followed by process environment.

The files overlay then wins only for the files target.

`stop_files.py` uses the same base, environment, and overlay model.

`bridge_control.py` instead makes values in each profile win over process environment.

Its standalone profiles do not automatically inherit an overlay.

With no `--env-file`, `bridge_control.py` uses the two profiles next to the helper.

It accepts repeated `--env-file`; no arguments target both tunnel profiles only, never Comfy.

The supplied pasted `restart_files.py` loads base dotenv with an existing process environment winning, then overwrites with the files overlay.

For its target it uses `FTPS_FILES_DIR`, else blindly appends `/files` to post-overlay `FTPS_DIR`.

That fallback can produce `/files/files`; set `FTPS_FILES_DIR` explicitly.

`restart_files.py` parses argparse before dotenv loading, so dotenv retry/timeout defaults come only from the pre-existing process environment.

Use explicit `--retries` and `--ftps-timeout` for predictable values.

Its supplied `FTPS_TLS` path has no explicit verified SSL context; do not claim verified certificates for it.

New `stop_files.py`, all senders, the helper, and the corrected patch use a default verified context.

### Start the receiver bridges

On Windows, use two separate terminals in `bridge/`:

```powershell
python cf_quicktunnel_writer.py --env-file .env
```

```powershell
python cf_quicktunnel_writer.py --env-file .env.files
```

The Python bridge processes must remain alive for a later remote restart request to work.

If they are stopped, flags cannot remotely start them; use an OS service or autostart arrangement when unattended recovery is required.

A tunnel pause disables automatic health-triggered restart.

The local HTTP server may still answer while cloudflared is paused.

Therefore a local `GET 200` is not proof that the public tunnel is live.

### Discovery, routes, and URL changes

Consult `cf_quicktunnel_writer.py` for the actual discovery path and schema.

The original writer behavior observed `tunnel_url.json` and text output in `OUT_DIR`, with upload to `FTPS_DIR` when enabled.

Fresh Quick Tunnel URLs change after a restart.

Discovery clearing during pause/stop is best effort, not a delivery guarantee.

`/health` is bridge-only and is not a ComfyUI health assertion.

Optional image, 3D, mesh, and video routes depend on the writer implementation.

`?list=json` depends on `FILES_INDEX`; byte-range behavior depends on `FILES_RANGE` and implementation.

### Remote control mailbox

Use a sender machine, for example Ubuntu, to upload small JSON requests through FTPS.

No remote shell command is sent or executed by the sender.

The receiver polls the mailbox and executes its configured Comfy commands locally.

Polling is independent per receiver and is commonly configured for roughly 30 seconds; a Comfy launch can take longer.

No transaction, readiness acknowledgement, or process confirmation is provided.

A sender tries all targets even if an earlier target fails, so partial failure is possible.

For each target, it deletes the opposite flag only if present, then uploads the requested flag.

No local control files or remote directories are created.

All control JSON is built in memory.

The required remote tree is:

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

The corrected root handler applies root flags only to the tunnel, even when its action is `both`.

Only the `comfy/` subdirectory controls ComfyUI.

For root tunnel flags, stop takes precedence over a conflicting restart.

A deleted flag is not proof that the requested action succeeded.

Typical receiver logs describe a root flag as detected and the tunnel as pausing or restarting.

### Sender command table

Run `python3` on Ubuntu-like systems and `python` on Windows.

| Intent | Ubuntu example | Windows example |
|---|---|---|
| Stop all: files tunnel, main tunnel, Comfy | `python3 stop_all.py` | `python stop_all.py` |
| Restart all: Comfy, files tunnel, main tunnel | `python3 restart_all.py` | `python restart_all.py` |
| Validate all targets, no upload | `python3 stop_all.py --dry-run` | `python stop_all.py --dry-run` |
| Stop both tunnel profiles only | `python3 stop_bridge.py` | `python stop_bridge.py` |
| Restart both tunnel profiles only | `python3 restart_bridge.py` | `python restart_bridge.py` |
| Stop only main tunnel | `python3 stop_bridge.py --env-file .env` | `python stop_bridge.py --env-file .env` |
| Restart only files tunnel | `python3 restart_bridge.py --env-file .env.files` | `python restart_bridge.py --env-file .env.files` |
| Stop only files tunnel | `python3 stop_files.py --env-file .env --files-env .env.files` | `python stop_files.py --env-file .env --files-env .env.files` |
| Restart only files tunnel | `python3 restart_files.py --env-file .env --files-env .env.files` | `python restart_files.py --env-file .env --files-env .env.files` |
| Only ComfyUI | use the supplied Comfy sender/version documentation | use the supplied Comfy sender/version documentation |

This guide assumes `stop_bridge.py` and `restart_bridge.py` wrappers delegate to `bridge_control.main`.

Older standalone `stop_bridge` versions can differ.

For unknown standalone Comfy sender details, use the original `--env-file .env --message "..."` style and inspect `--help` for that version.

### All-sender options and examples

`stop_all.py` and `restart_all.py` support the following options.

| Option | Meaning |
|---|---|
| `--env-file PATH` | Explicit main/base dotenv file. |
| `--files-env PATH` | Explicit files overlay dotenv file. |
| `--no-auto-env` | Do not discover `.env` and `.env.files`. |
| `--message TEXT` | Include optional request text in the in-memory JSON. |
| `--retries N` | Positive transfer attempts for each target. |
| `--ftps-timeout SECONDS` | Positive finite FTPS timeout. |
| `--dry-run` | Validate and print targets; do not upload or connect. |

Use explicit paths and values when operating outside the bridge directory:

```bash
python3 stop_all.py --env-file .env --files-env .env.files --message "maintenance" --retries 5 --ftps-timeout 30
```

```bash
python3 restart_all.py --env-file .env --files-env .env.files --message "resume" --retries 5 --ftps-timeout 30
```

The all-sender stop upload order is files tunnel, main tunnel, then Comfy.

The all-sender restart upload order is Comfy, files tunnel, then main tunnel.

Those are upload orders, not execution orders: separate pollers observe flags independently.

### Exit codes and lifecycle scripts

For all senders, exit `0` means every requested upload succeeded, or a dry run completed validation without uploads.

Exit `1` means a transfer failure or interruption.

Exit `2` means configuration or argument parsing failure.

An upload success never confirms receiver action.

The local lifecycle scripts retain their own documented exit conventions: `0` success, `1` failure, and `2` already running.

Interpret those codes for the respective lifecycle script, not as upload semantics.

`-ForceStop` can interrupt work in progress.

### Troubleshooting

**Flags are never detected.** Check the exact target directory, account permissions for `LIST`/`NLST`, write, and delete, and that both receiver processes are still running.

**Files commands hit `/files/files`.** Define `FTPS_FILES_DIR=/web-root/bridge/files` explicitly in the relevant profile.

**The tunnel seems healthy locally but is unavailable publicly.** A local health `GET 200` does not prove a public Quick Tunnel; inspect cloudflared and bridge logs.

**ComfyUI repeatedly starts or stops.** Install the corrected control patch, remove stale flags, and avoid concurrent controllers.

**Upload succeeded but nothing happened.** Upload confirmation is not execution confirmation; allow the polling interval and check receiver logs and configured commands.

**Restart cannot revive the bridge.** Flags can control a running bridge only; use OS service/autostart for a terminated Python process.

**Sender behavior differs.** Confirm script generation and run `--help`; old standalone helpers and pasted files senders are not interchangeable.

### Git hygiene

Keep local secrets and generated discovery output out of version control:

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
