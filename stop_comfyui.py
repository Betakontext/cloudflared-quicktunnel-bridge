# stop_comfyui.ps1 (tunnel-safe micro+bind)
# Stops ComfyUI reliably and ensures port is actually free.
# - Counts only LISTENING/ESTABLISHED as holders (ignores TIME_WAIT/CLOSE_WAIT).
# - Optional bind-probe on 127.0.0.1:PORT before declaring failure.
# - Always removes lock files if present.
#
# Exit codes:
#   0 = success (stopped or already not running; port is free)
#   1 = invalid arguments/paths
#   3 = failed to stop existing processes
#   4 = port did not free in time (and bind-probe failed)
#   7 = unexpected error
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File ".\stop_comfyui.ps1"

[CmdletBinding()]
param(
  [string]$ComfyRoot = "C:\Users\Administrator\Documents\Arbeiten\0000_DEV\ComfyUI",
  [int]$Port = 8188,
  [int]$KillTimeoutSec = 20,
  [int]$PortWaitTimeoutSec = 45,
  [switch]$AggressivePortFree = $true,
  [string]$RestartLock = ".restart.lock",
  [string]$StopLock = ".stop.lock",
  [string]$LogPrefix = "[comfy-stop]"
)

function Write-Log { param([string]$msg)
  $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  Write-Host "$LogPrefix $ts $msg"
}

function Netstat-LinesForPort { param([int]$p)
  try { & netstat -ano | Select-String -Pattern (":$p\s") } catch { @() }
}

function Test-PortFree { param([int]$p)
  $lines = Netstat-LinesForPort -p $p
  if (-not $lines -or $lines.Count -eq 0) { return $true }
  foreach ($line in $lines) {
    $cols = ($line -replace '\s+', ' ').Trim().Split(' ')
    if ($cols.Count -ge 5) {
      $state = $cols[$cols.Count - 2]
      if ($state -match 'LISTENING|ESTABLISHED') { return $false }
    }
  }
  return $true
}

function Get-PortHolderPids { param([int]$p)
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
  ($pids | Sort-Object -Unique)
}

function Wait-PortFree { param([int]$p, [int]$TimeoutSec)
  Write-Log ("Waiting for port " + $p + " to become free (timeout " + $TimeoutSec + "s)...")
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
    if (Test-PortFree -p $p) { Write-Log ("Port " + $p + " is free."); return $true }
    Start-Sleep -Milliseconds 350
  }
  return (Test-PortFree -p $p)
}

function Bind-Probe { param([int]$p)
  # Try to bind 127.0.0.1:p in a disposable TcpListener. If succeeds → port is practically free.
  try {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse("127.0.0.1"), $p)
    $listener.Start()
    $listener.Stop()
    return $true
  } catch {
    return $false
  }
}

function Get-ComfyProcesses { param([string]$Root)
  try {
    $q = "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='python.exe'"
    $procs = Get-WmiObject -Query $q -ErrorAction Stop
    $escRoot = [Regex]::Escape($Root)
    $matches = $procs | Where-Object { $_.CommandLine -ne $null -and $_.CommandLine -match $escRoot }
    return $matches
  } catch {
    return @()
  }
}

function Stop-ComfyProcesses { param([int[]]$ProcIds, [int]$TimeoutSec)
  if (-not $ProcIds -or $ProcIds.Count -eq 0) { return $true }
  Write-Log ("Stopping ComfyUI processes: " + ($ProcIds -join ', '))
  foreach ($pid in $ProcIds) {
    try { Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue } catch {}
  }
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
    Start-Sleep -Milliseconds 300
    $still = @()
    foreach ($pid in $ProcIds) {
      try { if (Get-Process -Id $pid -ErrorAction SilentlyContinue) { $still += $pid } } catch {}
    }
    if ($still.Count -eq 0) { return $true }
  }
  $rem = @()
  foreach ($pid in $ProcIds) {
    try { if (Get-Process -Id $pid -ErrorAction SilentlyContinue) { $rem += $pid } } catch {}
  }
  if ($rem.Count -gt 0) {
    Write-Log ("Failed to stop PIDs: " + ($rem -join ', '))
    return $false
  }
  return $true
}

function Kill-PortHolders { param([int]$p)
  $pids = Get-PortHolderPids -p $p
  if (-not $pids -or $pids.Count -eq 0) {
    Write-Log ("No explicit LISTEN/ESTABLISHED holders for " + $p + " found.")
    return
  }
  Write-Log ("Killing port " + $p + " holders: " + ($pids -join ', '))
  foreach ($pid in $pids) {
    try { Stop-Process -Id $pid -Force -ErrorAction Stop } catch {
      Write-Log ("WARNING: Failed to kill PID " + $pid + ": " + $_.Exception.Message)
    }
  }
}

# Main
$stopLockPath = $null
try {
  if (-not (Test-Path $ComfyRoot)) { Write-Log ("ERROR: ComfyRoot not found: " + $ComfyRoot); exit 1 }

  # Create stop lock (best-effort)
  $stopLockPath = Join-Path $ComfyRoot $StopLock
  try { New-Item -ItemType File -Path $stopLockPath -Force | Out-Null } catch {}

  $existing = Get-ComfyProcesses -Root $ComfyRoot
  if ($existing.Count -gt 0) {
    $procIds = ($existing | Select-Object -ExpandProperty ProcessId)
    if (-not (Stop-ComfyProcesses -ProcIds $procIds -TimeoutSec $KillTimeoutSec)) {
      Write-Log "ERROR: Failed to stop some ComfyUI processes."
      exit 3
    }
  } else {
    Write-Log "No ComfyUI processes found."
  }

  $portFree = Wait-PortFree -p $Port -TimeoutSec $PortWaitTimeoutSec
  if (-not $portFree -and $AggressivePortFree) {
    Write-Log "Port still not free → killing explicit LISTEN/ESTABLISHED holders and retrying shortly."
    Kill-PortHolders -p $Port
    Start-Sleep -Seconds 2
    $portFree = Test-PortFree -p $Port
  }

  if (-not $portFree) {
    # As last resort, try bind-probe
    if (Bind-Probe -p $Port) {
      Write-Log ("Bind-probe succeeded; treating port " + $Port + " as free.")
      exit 0
    } else {
      # Log current holders to help diagnose
      $holders = Get-PortHolderPids -p $Port
      if ($holders.Count -gt 0) {
        Write-Log ("Active holders for port " + $Port + ": " + ($holders -join ', '))
        foreach ($pid in $holders) {
          try {
            $p = Get-Process -Id $pid -ErrorAction SilentlyContinue
            if ($p) { Write-Log ("PID " + $pid + " - " + $p.ProcessName + " (" + $p.Path + ")") }
          } catch {}
        }
      }
      Write-Log ("ERROR: Port " + $Port + " did not free in time.")
      exit 4
    }
  }

  Write-Log "Stopped. Port is free."
  exit 0

} catch {
  Write-Log ("ERROR: Unexpected: " + $_.Exception.Message)
  exit 7

} finally {
  # Remove locks
  try {
    if ($stopLockPath -and (Test-Path $stopLockPath)) {
      Remove-Item $stopLockPath -ErrorAction SilentlyContinue
      Write-Log ("Removed stop lock: " + $stopLockPath)
    }
    $restartLockPath = Join-Path $ComfyRoot $RestartLock
    if (Test-Path $restartLockPath) {
      Remove-Item $restartLockPath -ErrorAction SilentlyContinue
      Write-Log ("Removed restart lock (if any): " + $restartLockPath)
    }
  } catch {}
}
