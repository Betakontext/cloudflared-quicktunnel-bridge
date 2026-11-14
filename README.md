### Cloudflared Quick Tunnel URL Bridge

Access a local ComfyUI instance and its generated files through Cloudflare Quick Tunnels—even behind CGNAT, without router port forwarding.

The script detects each tunnel URL, saves it locally, and optionally publishes it to your webspace via FTPS. Remote applications fetch a stable discovery JSON to find the current address.

**Python standard library only. Requires cloudflared. No built-in authentication.**

#### Architecture

Run two instances with separate configurations:

| Instance | Tunnel target | Discovery URL |
|---|---|---|
| A: ComfyUI | `http://127.0.0.1:8188` | `https://your-host.example/bridge/tunnel_url.json` |
| B: Files | `http://127.0.0.1:8800` | `https://your-host.example/bridge/files/tunnel_url.json` |

FTPS publishes the addresses only. Application traffic and downloads pass through the tunnels.

#### Requirements

- Python compatible with the script.
- cloudflared installed locally or available on `PATH`.
- Running ComfyUI instance.
- Optional: explicit FTPS hosting with publicly accessible HTTPS discovery files.

#### Configuration

Create `.env` for Tunnel A:

    CLOUDFLARED=.\cloudflared-windows-amd64.exe
    COMFY_URL=http://127.0.0.1:8188
    OUT_DIR=./bridge_output/comfy
    HTTP_HOST=127.0.0.1
    HTTP_PORT=8799
    HTTP_CORS=false
    EDGE_PROTOCOL=http2
    FILES_ENABLE=false
    FTPS_ENABLE=true
    FTPS_HOST=ftp.your-host.example
    FTPS_USER=your-user
    FTPS_PASS=your-password
    FTPS_DIR=/your-web-root/bridge
    FTPS_RETRIES=5

Create `.env.files` for Tunnel B:

    CLOUDFLARED=.\cloudflared-windows-amd64.exe
    COMFY_URL=http://127.0.0.1:8800
    OUT_DIR=./bridge_output/files
    HTTP_HOST=127.0.0.1
    HTTP_PORT=8800
    HTTP_CORS=false
    EDGE_PROTOCOL=http2
    FILES_ENABLE=true
    FILES_ROOT=C:/path/to/ComfyUI/output
    FILES_INDEX=true
    FILES_RANGE=true
    FTPS_ENABLE=true
    FTPS_HOST=ftp.your-host.example
    FTPS_USER=your-user
    FTPS_PASS=your-password
    FTPS_DIR=/your-web-root/bridge/files
    FTPS_RETRIES=5

Adjust paths and hosting credentials. On Linux/macOS, configure the appropriate cloudflared executable.

**Important:**

- Use separate HTTP ports, output directories, and FTPS directories.
- Tunnel B's `COMFY_URL` must point to its own bridge HTTP server.
- Public HTTPS paths depend on your hosting configuration.
- Set `FTPS_ENABLE=false` to disable publishing.
- Put `.env` comments on separate lines; inline comments are not supported.
- Existing environment variables take precedence over `.env` values.

#### Run

Start ComfyUI, then launch each instance in a separate terminal:

    python cf_quicktunnel_writer.py

    python cf_quicktunnel_writer.py --env-file ".env.files"

Show available options:

    python cf_quicktunnel_writer.py --help

Each instance writes `tunnel_url.json` and `tunnel_url.txt` to its output directory.

JSON format:

    {
      "url": "https://example.trycloudflare.com",
      "updated_at": "2026-11-14T17:34:08Z"
    }

#### Remote access

Fetch the appropriate discovery JSON and use its `url` as the base address.

Tunnel A provides ComfyUI access.

Tunnel B provides file routes:

| Route | Default directory |
|---|---|
| `/files/images/` | `FILES_ROOT` |
| `/files/3d/` | `FILES_ROOT/3d` |
| `/files/mesh/` | `FILES_ROOT/mesh` |
| `/files/video/` | `FILES_ROOT/video` |

Examples:

    /files/images/example.png
    /files/3d/example.glb
    /files/images/?list=json
    /files/mesh/?list=json

Directory listings require `FILES_INDEX=true`. There is no combined listing at `/files/`.

The `images` route does not filter file types. Only configure directories intended for sharing.

#### Bridge endpoints

Available on each instance's configured HTTP port:

- `/bridge/tunnel_url.json`
- `/bridge/tunnel_url.txt`
- `/health`
- `/`

`/health` reports process state, not end-to-end ComfyUI or tunnel readiness.

`HTTP_CORS=true` affects the bridge server only, not ComfyUI exposed through Tunnel A.

#### Security and limitations

- Public tunnel addresses are not authentication. Protect ComfyUI and file access separately.
- Keep credentials private and run with minimal permissions.
- FTPS is explicit FTP over TLS, not SFTP.
- Review TLS certificate verification before production use.
- Quick Tunnel addresses can change after restarts.
- Discovery files can become stale; `updated_at` is not a heartbeat.
- FTPS uploads currently block tunnel-log reading during retries.
- Failed uploads are not continuously rescheduled.
- Remote files are overwritten directly rather than published atomically.
- Refresh discovery data after connection failures.
- Do not blindly resend generation requests after timeouts.

For permanent deployments, consider a named tunnel with a fixed hostname and an access-control layer.

#### Troubleshooting

| Problem | Check |
|---|---|
| cloudflared not found | Executable path or `PATH` |
| HTTP port already in use | Different `HTTP_PORT` per instance |
| Tunnel B opens ComfyUI | Set its target to `http://127.0.0.1:8800` |
| Discovery files overwrite each other | Separate `OUT_DIR` and `FTPS_DIR` |
| FTPS `451` write error | Hosting space, quota, permissions, server logs |
| `/files/` returns `404` | Use a category route |
| Logs pause during upload | Upload retries currently block log reading |

#### Git hygiene

Recommended `.gitignore`:

    .env
    .env.*
    !.env.example
    !.env.files.example
    bridge_output/
    __pycache__/
    *.py[cod]

If credentials were committed, rotate them—even after removing the files.

#### GitHub topics

`python`, `comfyui`, `cloudflared`, `cloudflare-tunnel`, `quick-tunnel`, `cgnat`, `remote-access`, `ftps`, `service-discovery`, `file-server`

#### Suggested issue labels

`bug`, `enhancement`, `documentation`, `security`, `cloudflared`, `ftps`, `file-server`, `comfyui`, `configuration`, `reliability`, `windows`, `linux`, `macos`, `good first issue`, `help wanted`

Report security vulnerabilities privately.

#### License and contact

MIT — see `LICENSE`.

Provided as-is. Follow your Cloudflare and hosting provider terms.

- Contact: dev@betakontext.de
- Website: https://dev.betakontext.de
- Support: https://buymeacoffee.com/betakontext
