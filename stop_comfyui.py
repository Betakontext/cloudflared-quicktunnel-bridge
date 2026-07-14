# stop_comfyui.ps1 (tunnel-safe micro + bind-probe)
# Stops ComfyUI reliably with stale-lock handling, deterministic kill, and robust port verification
# while leaving Cloudflared/Quicktunnel processes untouched.
#
# Enhancements over your baseline:
# - Port usage detection counts only LISTENING/ESTABLISHED; transitional states ignored.
# - Final bind-probe: if we can bind 127.0.0.1:<Port> briefly, the port is effectively free → success.
# - Before exit 4, logs real LISTEN/ESTABLISHED holders (PID, process name, optional path).
# - Lock cleanup remains guaranteed in finally.
#
# Exit codes:
#   0 = success or already stopped
#   2 = could not acquire lock (active other op)
#   4 = port did not free in time (real listener stayed)
#   5 = unexpected error
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File ".\stop_comfyui.ps1"

[CmdletBinding()]
param(
  # Paths
  [string]$ComfyRoot = "C:\Users\Administrator\Documents\Arbeiten\0000_DEV\ComfyUI",
  [string]$LockFile = ".restart.lock",

  # Network
  [int]$Port = 8188,

  # Process match (used to identify Comfy python processes)
  [string]$MainPy = "",  # optional; if empty, we match by ComfyRoot directory
  [string]$ProcName = "python.exe",

  # Timeouts
  [int]$KillTimeoutSec = 20,
  [int]$PortWaitTimeoutSec = 60,

  # Logging
  [string]$LogPrefix = "[comfy-stop]",

  # Tuning flags
  [int]$LockStaleSec = 180,       # consider lock stale after N seconds
  [switch]$ForceUnlock = $false,  # remove existing lock unconditionally
  [switch]$AggressivePortFree = $true # kill any process holding the port if initial wait fails
)

# --------------- Utilities ---------------

function Write-Log {
  param([string]$msg)
  $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  Write-Host "$LogPrefix $ts $msg"
}

function Fail-And-Exit {
  param([int]$code, [string]$msg)
  Write-Log "ERROR: $msg"
  throw [System.Exception]::new("EXIT_CODE::$code")
}

function Resolve-MainPy {
  param([string]$Root, [string]$Main)
  if ([string]::IsNullOrWhiteSpace($Main)) {
    return (Join-Path $Root "main.py")
  }
  return $Main
}

function Get-ComfyProcesses {
  # Match python.exe processes whose CommandLine references main.py or its containing directory.
  param([string]$MainPyPath, [string]$ProcNameFilter = "python.exe")
  try {
    $q = "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='" + $ProcNameFilter + "'"
    $procs = Get-WmiObject -Query $q -ErrorAction Stop
    $mainDir = [System.IO.Path]::GetDirectoryName($MainPyPath)
    $escMain = [Regex]::Escape($MainPyPath)
    $escDir  = [Regex]::Escape($mainDir)
    $matches = $procs | Where-Object {
      $_.CommandLine -ne $null -and
      ($_.CommandLine -match $escMain -or $_.CommandLine -match $escDir)
    }
    return $matches
  } catch {
    return @()
  }
}

function Stop-ByPidList {
  param([int[]]$ProcIds, [int]$TimeoutSec = 20)
  if (-not $ProcIds -or $ProcIds.Count -eq 0) { return $true }
  Write-Log ("Stopping existing processes: " + ($ProcIds -join ', '))
  foreach ($ProcId in $ProcIds) {
    try { Stop-Process -Id $ProcId -Force -ErrorAction SilentlyContinue } catch {}
  }
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
    Start-Sleep -Milliseconds 300
    $still = @()
    foreach ($ProcId in $ProcIds) {
      try { if (Get-Process -Id $ProcId -ErrorAction SilentlyContinue) { $still += $ProcId } } catch {}
    }
    if ($still.Count -eq 0) { return $true }
  }
  $rem = @()
  foreach ($ProcId in $ProcIds) {
    try { if (Get-Process -Id $ProcId -ErrorAction SilentlyContinue) { $rem += $ProcId } } catch {}
  }
  if ($rem.Count -gt 0) {
    Write-Log ("WARNING: Failed to stop PIDs within timeout: " + ($rem -join ', '))
    # We continue; port check will decide
  }
  return $true
}

# netstat helpers: only LISTENING/ESTABLISHED count as "in use"
function Netstat-LinesForPort {
  param([int]$p)
  try {
    return & netstat -ano | Select-String -Pattern (":$p\s")
  } catch { return @() }
}

function Test-PortFree {
  param([int]$p)
  $lines = Netstat-LinesForPort -p $p
  if (-not $lines -or $lines.Count -eq 0) { return $true }
  foreach ($line in $lines) {
    $cols = ($line -replace '\s+', ' ').Trim().Split(' ')
    # Expected: Proto LocalAddr ForeignAddr State PID
    if ($cols.Count -ge 5) {
      $state = $cols[$cols.Count - 2]
      if ($state -match 'LISTENING|ESTABLISHED') { return $false }
    }
  }
  return $true
}

function Wait-PortFree {
  param([int]$p, [int]$TimeoutSec)
  Write-Log ("Waiting for port " + $p + " to become free (timeout " + $TimeoutSec + "s)...")
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
    if (Test-PortFree -p $p) { Write-Log ("Port " + $p + " is free."); return $true }
    Start-Sleep -Milliseconds 350
  }
  return (Test-PortFree -p $p)
}

function Get-PortHolderPids {
  param([int]$p)
  $pids = @()
  $lines = Netstat-LinesForPort -p $p
  foreach ($line in $lines) {
    $cols = ($line -replace '\s+', ' ').Trim().Split(' ')
    if ($cols.Count -ge 5) {
      $state = $cols[$cols.Count - 2]
      $pidStr = $cols[$cols.Count - 1]
      if ($state -match 'LISTENING|ESTABLISHED' -and $pidStr -match '^\d+$') {
        $pids += [int]$pidStr
      }
    }
  }
  return ($pids | Sort-Object -Unique)
}

function Log-PortHolders {
  param([int]$p)
  $holders = Get-PortHolderPids -p $p
  if ($holders -and $holders.Count -gt 0) {
    Write-Log ("Port " + $p + " held by PIDs: " + ($holders -join ', '))
    foreach ($pid in $holders) {
      try {
        $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
        if ($proc) {
          $path = $null; try { $path = $proc.Path } catch {}
          Write-Log ("Holder PID " + $pid + " => " + $proc.ProcessName + (if ($path) { " [$path]" } else { "" }))
        }
      } catch {}
    }
  } else {
    Write-Log ("No LISTEN/ESTABLISHED holders detected; likely transitional states only.")
  }
}

# Final authoritative check: try to bind the port briefly. If we can bind, the port is free.
function Try-BindProbe {
  param([int]$p)
  try {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $p)
    $listener.Start()
    $listener.Stop()
    return $true
  } catch {
    return $false
  }
}

# --------------- Main ---------------

$lockPath = $null
$lockCreated = $false
try {
  # Validate paths
  if (-not (Test-Path $ComfyRoot)) {
    Write-Log ("ComfyRoot not found: " + $ComfyRoot + " → assuming not running.")
    exit 0
  }
  $resolvedMain = Resolve-MainPy -Root $ComfyRoot -Main $MainPy

  # Acquire/remove stale lock
  $lockPath = Join-Path $ComfyRoot $LockFile
  if (Test-Path $lockPath) {
    Write-Log ("Lock file exists at " + $lockPath + ". Checking staleness...")
    if ($ForceUnlock) {
      Write-Log "FORCE_UNLOCK set → removing existing lock."
      Remove-Item $lockPath -ErrorAction SilentlyContinue
    } else {
      $ageSec = [int]((Get-Date) - (Get-Item $lockPath).LastWriteTime).TotalSeconds
      if ($ageSec -gt $LockStaleSec) {
        Write-Log ("Lock is stale (age " + $ageSec + "s > " + $LockStaleSec + "s). Removing.")
        Remove-Item $lockPath -ErrorAction SilentlyContinue
      } else {
        Fail-And-Exit 2 ("Another operation in progress (lock: " + $lockPath + ", age " + $ageSec + "s)")
      }
    }
  }
  New-Item -ItemType File -Path $lockPath -Force | Out-Null
  $lockCreated = $true
  Write-Log ("Acquired lock: " + $lockPath)

  # Detect and stop Comfy-related processes (tunnels remain untouched)
  $existing = Get-ComfyProcesses -MainPyPath $resolvedMain -ProcNameFilter $ProcName
  if ($existing.Count -eq 0) {
    Write-Log "No matching ComfyUI processes found."
  } else {
    $pids = ($existing | Select-Object -ExpandProperty ProcessId)
    if (-not (Stop-ByPidList -ProcIds $pids -TimeoutSec $KillTimeoutSec)) {
      Write-Log "WARNING: Some processes may still be running."
    }
  }

  # Wait for port to free (ignores transitional states)
  $portFree = Wait-PortFree -p $Port -TimeoutSec $PortWaitTimeoutSec

  # If still not free, try to clear explicit LISTEN/ESTABLISHED holders (if allowed)
  if (-not $portFree -and $AggressivePortFree) {
    Write-Log "Port still not free → attempting to kill explicit LISTEN/ESTABLISHED holders."
    Log-PortHolders -p $Port
    $holders = Get-PortHolderPids -p $Port
    if ($holders -and $holders.Count -gt 0) {
      foreach ($pid in $holders) {
        try { Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue } catch {
          Write-Log ("WARNING: Failed to kill PID " + $pid + ": " + $_.Exception.Message)
        }
      }
      Start-Sleep -Seconds 2
    }
    $portFree = Test-PortFree -p $Port
  }

  # Final bind-probe: if we can bind, then the port is effectively free
  if (-not $portFree) {
    if (Try-BindProbe -p $Port) {
      Write-Log ("Bind-probe succeeded; port " + $Port + " is effectively free.")
      $portFree = $true
    }
  }

  if (-not $portFree) {
    # Final diagnostics before exit
    Log-PortHolders -p $Port
    Fail-And-Exit 4 ("Port " + $Port + " did not free within " + $PortWaitTimeoutSec + "s.")
  }

  Write-Log "Stop completed successfully."
  exit 0

} catch {
  # Parse our Fail-And-Exit code if present
  $code = 5
  $msg = $_.Exception.Message
  if ($msg -like "EXIT_CODE::*") {
    try { $code = [int]($msg.Split("::")[-1]) } catch {}
  } elseif ($msg -like "*EXIT_CODE::*") {
    try { $code = [int]($msg.Substring($msg.LastIndexOf("EXIT_CODE::") + 10)) } catch {}
  } else {
    # keep default
  }
  Write-Log ("Aborting with code " + $code + ". Reason: " + $msg)
  exit $code

} finally {
  # Always cleanup lock to prevent stuck lifecycle
  try {
    if ($lockCreated -and (Test-Path $lockPath)) {
      Remove-Item $lockPath -ErrorAction SilentlyContinue
      Write-Log ("Lock removed in finally: " + $lockPath)
    }
  } catch {}
}