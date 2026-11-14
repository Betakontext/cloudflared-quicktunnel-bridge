# stop_comfyui.ps1
# Stop ComfyUI through the shared lifecycle implementation.
# Keep restart_comfyui.ps1 in the same directory.
# ForceStop interrupts active jobs.
# Exit codes: 0 success, 1 failure, 2 operation already running.

[CmdletBinding()]
param(
    [string]$ComfyRoot = "",
    [string]$MainPy = "",

    [ValidateRange(1, 65535)]
    [int]$Port = 8188,

    [switch]$ForceStop,

    [ValidateRange(1, 300)]
    [int]$StopTimeoutSec = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$implementation = Join-Path $PSScriptRoot "restart_comfyui.ps1"

try {
    if (-not (Test-Path -LiteralPath $implementation -PathType Leaf)) {
        throw "Required script not found: $implementation"
    }

    # Forward resolved defaults as well as explicitly supplied parameters.
    $parameters = @{
        Action         = "Stop"
        ComfyRoot      = $ComfyRoot
        MainPy         = $MainPy
        Port           = $Port
        ForceStop      = [bool]$ForceStop
        StopTimeoutSec = $StopTimeoutSec
    }

    & $implementation @parameters
    exit $LASTEXITCODE
}
catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
