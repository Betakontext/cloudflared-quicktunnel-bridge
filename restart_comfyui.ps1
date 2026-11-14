# restart_comfyui.ps1
# Local ComfyUI lifecycle management for Windows.
# Requires Windows PowerShell 5.1 or PowerShell 7.
# Exit codes: 0 success, 1 failure, 2 operation already running.
# ForceStop interrupts active jobs. SQLite files are never deleted.

[CmdletBinding()]
param(
    [ValidateSet("Restart", "Stop")]
    [string]$Action = "Restart",

    [string]$ComfyRoot = "",
    [string]$VenvPython = "",
    [string]$MainPy = "",

    [ValidateRange(1, 65535)]
    [int]$Port = 8188,

    [ValidateSet("127.0.0.1", "0.0.0.0")]
    [string]$Listen = "127.0.0.1",

    [switch]$LowVRAM,
    [switch]$ForceStop,

    [ValidateRange(5, 3600)]
    [int]$HealthTimeoutSec = 180,

    [ValidateRange(1, 300)]
    [int]$StopTimeoutSec = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$exitCode = 1
$mutex = $null
$lockAcquired = $false

function Write-Log {
    param([string]$Message)

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[comfy-$($Action.ToLowerInvariant())] $timestamp $Message"
}

function Resolve-RequiredPath {
    param(
        [string]$Path,
        [ValidateSet("Leaf", "Container")]
        [string]$Type
    )

    if (-not (Test-Path -LiteralPath $Path -PathType $Type)) {
        throw "Required path not found: $Path"
    }

    return (Resolve-Path -LiteralPath $Path).ProviderPath
}

function Get-ComfyProcesses {
    param([string]$MainPath)

    # Require an exact absolute main.py argument.
    # Do not match arbitrary processes containing only the directory name.
    $escaped = [regex]::Escape($MainPath)
    $pattern = '(?i)(?:^|\s)(?:"' + $escaped +
        '"|' + $escaped + ')(?=\s|$)'

    # Include pythonw.exe, but never match unrelated executable names.
    # Do not restrict ExecutablePath: a venv launcher may use base Python.
    $processes = Get-CimInstance -ClassName Win32_Process `
        -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" `
        -ErrorAction Stop

    foreach ($process in $processes) {
        if (
            $process.CommandLine -and
            $process.CommandLine -match $pattern
        ) {
            $process
        }
    }
}

function Test-PortFree {
    param([int]$LocalPort)

    # Only local TCP listeners matter; ignore TIME_WAIT and remote ports.
    $properties = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties()
    $listeners = $properties.GetActiveTcpListeners()
    $matching = @($listeners | Where-Object { $_.Port -eq $LocalPort })

    return ($matching.Count -eq 0)
}

function Wait-PortFree {
    param(
        [int]$LocalPort,
        [int]$TimeoutSec
    )

    $timer = [System.Diagnostics.Stopwatch]::StartNew()

    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSec) {
        if (Test-PortFree -LocalPort $LocalPort) {
            return $true
        }
        Start-Sleep -Milliseconds 300
    }

    return (Test-PortFree -LocalPort $LocalPort)
}

function Stop-VerifiedProcess {
    param(
        [object]$Candidate,
        [string]$MainPath,
        [int]$TimeoutSec
    )

    $processId = [int]$Candidate.ProcessId
    $target = Get-Process -Id $processId -ErrorAction SilentlyContinue

    if ($null -eq $target) {
        return
    }

    try {
        # Acquire a handle before rechecking process identity.
        try {
            $null = $target.Handle
        }
        catch {
            if ($target.HasExited) {
                return
            }
            throw
        }

        $verified = @(Get-ComfyProcesses -MainPath $MainPath |
            Where-Object {
                [int]$_.ProcessId -eq $processId -and
                $_.CreationDate -eq $Candidate.CreationDate
            })

        if ($verified.Count -ne 1) {
            if ($target.HasExited) {
                return
            }
            throw "Identity check failed for PID $processId; refusing termination."
        }

        if ($target.HasExited) {
            return
        }

        Write-Log "Terminating verified ComfyUI process $processId."

        try {
            $target.Kill()
        }
        catch {
            if (-not $target.HasExited) {
                throw
            }
        }

        if (-not $target.WaitForExit($TimeoutSec * 1000)) {
            throw "Process $processId did not exit within $TimeoutSec seconds."
        }
    }
    finally {
        $target.Dispose()
    }
}

try {
    # Resolve configuration after parameter binding.
    if ([string]::IsNullOrWhiteSpace($ComfyRoot)) {
        $ComfyRoot = $env:COMFY_ROOT
    }

    if ([string]::IsNullOrWhiteSpace($ComfyRoot)) {
        $ComfyRoot = Join-Path $PSScriptRoot "../ComfyUI"
    }

    # Anchor relative paths to this script, not the working directory.
    if (-not [System.IO.Path]::IsPathRooted($ComfyRoot)) {
        $ComfyRoot = Join-Path $PSScriptRoot $ComfyRoot
    }

    $ComfyRoot = Resolve-RequiredPath -Path $ComfyRoot -Type Container


    if ([string]::IsNullOrWhiteSpace($MainPy)) {
        $MainPy = Join-Path $ComfyRoot "main.py"
    }
    $MainPy = Resolve-RequiredPath -Path $MainPy -Type Leaf

    # Stop mode does not require an existing Python environment.
    if ($Action -eq "Restart") {
        if ([string]::IsNullOrWhiteSpace($VenvPython)) {
            $VenvPython = Join-Path $ComfyRoot ".venv\Scripts\python.exe"
        }
        $VenvPython = Resolve-RequiredPath -Path $VenvPython -Type Leaf
    }

    # Both actions share one machine-wide mutex per ComfyUI root.
    # No lock files or age-based lock deletion are needed.
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $rootKey = $ComfyRoot.TrimEnd('\', '/').ToLowerInvariant()
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($rootKey)
        $hash = [System.BitConverter]::ToString(
            $hasher.ComputeHash($bytes)
        ).Replace("-", "")
    }
    finally {
        $hasher.Dispose()
    }

    $mutex = [System.Threading.Mutex]::new(
        $false, "Global\ComfyUIBridge_$hash"
    )

    try {
        $lockAcquired = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        # An abandoned mutex is now owned by this process.
        $lockAcquired = $true
    }

    if (-not $lockAcquired) {
        $exitCode = 2
        throw "Another ComfyUI stop/restart operation is already running."
    }

    Write-Log "Acquired operation mutex."

    $existing = @(Get-ComfyProcesses -MainPath $MainPy)

    if ($existing.Count -gt 0) {
        if (-not $ForceStop) {
            throw (
                "ComfyUI is running. Use -ForceStop to permit termination. " +
                "Active jobs will be interrupted."
            )
        }

        foreach ($candidate in $existing) {
            Stop-VerifiedProcess -Candidate $candidate `
                -MainPath $MainPy -TimeoutSec $StopTimeoutSec
        }
    }
    else {
        Write-Log "No matching ComfyUI processes found."
    }

    Write-Log "Waiting for local TCP port $Port to become free."

    if (-not (Wait-PortFree -LocalPort $Port -TimeoutSec $StopTimeoutSec)) {
        throw (
            "Port $Port still has a listener. No unknown process was terminated. " +
            "Inspect its owner; ComfyUI started with a relative main.py argument " +
            "is intentionally not matched."
        )
    }

    if ($Action -eq "Stop") {
        Write-Log "ComfyUI is stopped; port $Port has no TCP listener."
        $exitCode = 0
    }
    else {
        $arguments = @(
            "`"$MainPy`"",
            "--listen", $Listen,
            "--port", "$Port",
            "--disable-auto-launch"
        )

        if ($LowVRAM) {
            $arguments += "--lowvram"
        }

        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $VenvPython
        $startInfo.WorkingDirectory = $ComfyRoot
        $startInfo.Arguments = $arguments -join " "
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $false
        $startInfo.EnvironmentVariables["PYTHONUNBUFFERED"] = "1"

        # Inherit console output without stdout/stderr redirection.
        $started = New-Object System.Diagnostics.Process

        try {
            $started.StartInfo = $startInfo

            Write-Log "Starting ComfyUI using $VenvPython"
            Write-Log "Arguments: $($startInfo.Arguments)"

            if (-not $started.Start()) {
                throw "Failed to launch ComfyUI."
            }

            Write-Log "Launcher PID: $($started.Id)"

            # This request stays on the local machine.
            $healthUrl = "http://127.0.0.1:$Port/system_stats"
            $timer = [System.Diagnostics.Stopwatch]::StartNew()
            $ready = $false

            while ($timer.Elapsed.TotalSeconds -lt $HealthTimeoutSec) {
                # A venv launcher may exit while a matching child remains alive.
                if ($started.HasExited) {
                    $remaining = @(Get-ComfyProcesses -MainPath $MainPy)
                    if ($remaining.Count -eq 0) {
                        throw "ComfyUI exited before becoming ready."
                    }
                }

                try {
                    $response = Invoke-WebRequest -Uri $healthUrl `
                        -UseBasicParsing -TimeoutSec 3 `
                        -MaximumRedirection 0 -ErrorAction Stop

                    if ([int]$response.StatusCode -eq 200) {
                        $stats = $response.Content | ConvertFrom-Json

                        if (
                            $null -ne $stats -and
                            $null -ne $stats.PSObject.Properties["system"]
                        ) {
                            $ready = $true
                            break
                        }
                    }
                }
                catch {
                    # Startup may take time while custom nodes are loading.
                }

                Start-Sleep -Seconds 2
            }

            if (-not $ready) {
                throw (
                    "HTTP readiness check timed out. No process was killed. " +
                    "Inspect the ComfyUI console before retrying."
                )
            }

            Write-Log "ComfyUI responds at http://127.0.0.1:$Port."
            $exitCode = 0
        }
        finally {
            # Disposing the handle does not terminate ComfyUI.
            $started.Dispose()
        }
    }
}
catch {
    Write-Log "ERROR: $($_.Exception.Message)"
}
finally {
    if ($lockAcquired -and $null -ne $mutex) {
        try {
            $mutex.ReleaseMutex()
        }
        catch {
            Write-Warning "Could not release mutex: $($_.Exception.Message)"
        }
    }

    if ($null -ne $mutex) {
        $mutex.Dispose()
    }
}

exit $exitCode
