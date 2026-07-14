# stop_comfyui.ps1
# Cleanly stops ComfyUI if running (no start). Safe lock, deterministic kill by CommandLine match,
# optional SQLite journal cleanup, and port-free confirmation.
#
# Exit codes:
#   0 = success (already stopped or stopped now)
#   1 = invalid arguments/paths
#   2 = could not acquire lock
#   3 = failed to stop existing processes
#   4 = port did not free in time
#   7 = unexpected error

[CmdletBinding()]
param(
  # Paths
  [string]$ComfyRoot = "C:\Users\Administrator\Documents\Arbeiten\0000_DEV\ComfyUI",
  [string]$MainPy = "",

  # Network
  [int]$Port = 8188,

  # Timeouts
  [int]$KillTimeoutSec = 20,
  [int]$PortWaitTimeoutSec = 60,

  # Options
  [switch]$CleanupSQLite = $true,   # remove db -wal/-shm if present

  # Lock + Logging
  [string]$LockFile = ".restart.lock",
  [string]$LogPrefix = "[comfy-stop]"
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
  exit $code
}

function Resolve-MainPy {
  param([string]$Root, [string]$Main)
  if ([string]::IsNullOrWhiteSpace($Main)) {
    return (Join-Path $Root "main.py")
  }
  return $Main
}

function Get-ComfyProcesses {
  param([string]$MainPyPath)
  try {
    $q = "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='python.exe'"
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

function Stop-ComfyProcesses {
  param([int[]]$ProcIds, [int]$TimeoutSec = 20)
  if (-not $ProcIds -or $ProcIds.Count -eq 0) { return $true }
  Write-Log ("Stopping existing ComfyUI processes: " + ($ProcIds -join ', '))
  foreach ($ProcId in $ProcIds) {
    try { Stop-Process -Id $ProcId -Force -ErrorAction SilentlyContinue } catch {}
  }
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
    Start-Sleep -Milliseconds 300
    $still = @()
    foreach ($ProcId in $ProcIds) {
      try {
        $p = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
        if ($p) { $still += $ProcId }
      } catch {}
    }
    if ($still.Count -eq 0) { return $true }
  }
  $rem = @()
  foreach ($ProcId in $ProcIds) {
    try { if (Get-Process -Id $ProcId -ErrorAction SilentlyContinue) { $rem += $ProcId } } catch {}
  }
  if ($rem.Count -gt 0) {
    Write-Log ("Failed to stop PIDs: " + ($rem -join ', '))
    return $false
  }
  return $true
}

function Test-PortFree {
  param([int]$p)
  try {
    $line = & netstat -ano | Select-String -SimpleMatch (":" + $p)
    return (-not $line)
  } catch {
    try {
      $client = New-Object System.Net.Sockets.TcpClient
      $iar = $client.BeginConnect("127.0.0.1", $p, $null, $null)
      $ok = $iar.AsyncWaitHandle.WaitOne(200)
      if ($ok -and $client.Connected) { $client.Close(); return $false }
      $client.Close(); return $true
    } catch { return $true }
  }
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

function Cleanup-SQLite-Journals {
  param([string]$Root)
  try {
    $userDir = Join-Path $Root "user"
    $wal = Join-Path $userDir "comfyui.db-wal"
    $shm = Join-Path $userDir "comfyui.db-shm"
    if (Test-Path $wal) { Remove-Item $wal -ErrorAction SilentlyContinue }
    if (Test-Path $shm) { Remove-Item $shm -ErrorAction SilentlyContinue }
    Write-Log "SQLite journals cleaned (.wal/.shm removed if present)."
  } catch {
    Write-Log ("WARNING: SQLite cleanup error: " + $_.Exception.Message)
  }
}

# --------------- Main ---------------

try {
  # Resolve and validate
  $MainPy = Resolve-MainPy -Root $ComfyRoot -Main $MainPy
  if (-not (Test-Path $ComfyRoot)) { Fail-And-Exit 1 ("ComfyRoot not found: " + $ComfyRoot) }
  if (-not (Test-Path $MainPy)) { Fail-And-Exit 1 ("main.py not found: " + $MainPy) }

  # Acquire lock
  $lockPath = Join-Path $ComfyRoot $LockFile
  if (Test-Path $lockPath) {
    Write-Log ("Lock file exists at " + $lockPath + ". Checking staleness...")
    $age = (Get-Date) - (Get-Item $lockPath).LastWriteTime
    if ($age.TotalMinutes -gt 10) {
      Write-Log "Lock older than 10 minutes; removing stale lock."
      Remove-Item $lockPath -ErrorAction SilentlyContinue
    } else {
      Fail-And-Exit 2 ("Another operation in progress (lock: " + $lockPath + ")")
    }
  }
  New-Item -ItemType File -Path $lockPath -Force | Out-Null
  $script:lockCreated = $true
  Write-Log ("Acquired lock: " + $lockPath)

  Register-EngineEvent PowerShell.Exiting -Action {
    try {
      if ($script:lockCreated -and (Test-Path $lockPath)) { Remove-Item $lockPath -ErrorAction SilentlyContinue }
    } catch {}
  } | Out-Null

  # Stop if running
  $existing = Get-ComfyProcesses -MainPyPath $MainPy
  if ($existing.Count -eq 0) {
    Write-Log "No ComfyUI processes found; nothing to stop."
  } else {
    $procIds = ($existing | Select-Object -ExpandProperty ProcessId)
    if (-not (Stop-ComfyProcesses -ProcIds $procIds -TimeoutSec $KillTimeoutSec)) {
      Fail-And-Exit 3 "Failed to stop some ComfyUI processes."
    }
  }

  # Wait port free
  if (-not (Wait-PortFree -p $Port -TimeoutSec $PortWaitTimeoutSec)) {
    Fail-And-Exit 4 ("Port " + $Port + " did not free within " + $PortWaitTimeoutSec + "s.")
  }

  # Optional cleanup
  if ($CleanupSQLite) {
    Cleanup-SQLite-Journals -Root $ComfyRoot
  }

  Write-Log "ComfyUI is stopped."

  # Success → release lock
  if (Test-Path $lockPath) { Remove-Item $lockPath -ErrorAction SilentlyContinue }
  $script:lockCreated = $false
  exit 0

} catch {
  try {
    if ($script:lockCreated -and (Test-Path $lockPath)) { Remove-Item $lockPath -ErrorAction SilentlyContinue }
  } catch {}
  Fail-And-Exit 7 ("Unhandled exception: " + $_.Exception.Message)
}
