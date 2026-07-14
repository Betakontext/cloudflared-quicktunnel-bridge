# restart_comfyui.ps1 (stale-lock resilient)
# Starts ComfyUI if not running, or restarts it when invoked by your flag watcher.
# Robust single-instance lock, deterministic kill, port wait, SQLite journal cleanup,
# start, and dependency-free health check using raw TCP connect.
#
# Enhancements over your baseline:
# - Stale-lock resilience: auto-remove lock if age > ShortStaleSec (default 45s).
# - One short retry if lock is fresh before exiting with code 2.
# - Same port-holder logic as stop (optional aggressive clear).
#
# Exit codes:
#   0 = success
#   1 = invalid arguments/paths
#   2 = could not acquire lock
#   3 = failed to stop existing processes
#   4 = port did not free in time
#   5 = start failed (process did not launch)
#   6 = health check failed
#   7 = unexpected error
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File ".\restart_comfyui.ps1"

[CmdletBinding()]
param(
  # Paths
  [string]$ComfyRoot = "C:\Users\Administrator\Documents\Arbeiten\0000_DEV\ComfyUI",
  [string]$VenvPython = "C:\Users\Administrator\Documents\Arbeiten\0000_DEV\ComfyUI\.venv\Scripts\python.exe",
  [string]$MainPy = "",

  # Network
  [string]$Listen = "0.0.0.0",
  [int]$Port = 8188,

  # Args
  [switch]$LowVRAM = $true,
  [switch]$DisableAutoLaunch = $true,
  [string]$DatabaseUrl = "",   # e.g. sqlite:///C:/path/to/comfyui_main.db

  # Health (TCP-based)
  [int]$HealthTimeoutSec = 120,
  [int]$HealthRetryDelaySec = 2,
  [int]$HealthInitialDelaySec = 1,
  [switch]$HttpConfirm = $false,

  # Timeouts
  [int]$KillTimeoutSec = 20,
  [int]$PortWaitTimeoutSec = 60,

  # Lock + Logging
  [string]$LockFile = ".restart.lock",
  [string]$LogPrefix = "[comfy-restart]",

  # Tuning flags
  [int]$LockStaleSec = 180,           # legacy stale threshold (kept for compatibility)
  [int]$ShortStaleSec = 45,           # new short stale threshold for auto-removal
  [switch]$ForceUnlock = $false,
  [switch]$AggressivePortFree = $true
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
      try { if (Get-Process -Id $ProcId -ErrorAction SilentlyContinue) { $still += $ProcId } } catch {}
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

# netstat helpers (same semantics as stop script)
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

function Kill-PortHolders {
  param([int]$p, [switch]$Force)
  $pids = Get-PortHolderPids -p $p
  if (-not $pids -or $pids.Count -eq 0) {
    Write-Log ("No explicit LISTEN/ESTABLISHED holders for " + $p + " found.")
    return
  }
  Write-Log ("Killing port " + $p + " holders: " + ($pids -join ', ') + " (force=" + $Force.IsPresent + ")")
  foreach ($pid in $pids) {
    try {
      if ($Force) { Stop-Process -Id $pid -Force -ErrorAction Stop }
      else { Stop-Process -Id $pid -ErrorAction Stop }
    } catch {
      Write-Log ("WARNING: Failed to kill PID " + $pid + ": " + $_.Exception.Message)
    }
  }
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

function Start-Comfy {
  param(
    [string]$VenvPythonPath,
    [string]$WorkingDir,
    [string]$MainPyPath,
    [string]$ListenAddr,
    [int]$ListenPort,
    [switch]$LowVRAM,
    [switch]$DisableAutoLaunch,
    [string]$DatabaseUrl
  )
  $argList = @()
  $argList += ('"' + $MainPyPath + '"')
  $argList += "--listen"; $argList += $ListenAddr
  $argList += "--port";   $argList += "$ListenPort"
  if ($LowVRAM) { $argList += "--lowvram" }
  if ($DisableAutoLaunch) { $argList += "--disable-auto-launch" }
  if ($DatabaseUrl -and $DatabaseUrl.Trim() -ne "") {
    $argList += "--database-url"; $argList += $DatabaseUrl
  }

  Write-Log "Starting ComfyUI..."
  Write-Log ("Venv: " + $VenvPythonPath)
  Write-Log ("Args: " + ($argList -join ' '))

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $VenvPythonPath
  $psi.WorkingDirectory = $WorkingDir
  $psi.Arguments = ($argList -join ' ')
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.CreateNoWindow = $true

  $proc = New-Object System.Diagnostics.Process
  $proc.StartInfo = $psi
  $null = $proc.Start()
  Write-Log ("ComfyUI started. ProcId=" + $proc.Id + ", listen=" + $ListenAddr + ", port=" + $ListenPort)

  # Background log pump (best-effort)
  try {
    Start-Job -ScriptBlock {
      param($hProcess)
      try {
        $readerOut = $hProcess.StandardOutput
        $readerErr = $hProcess.StandardError
        while (-not $hProcess.HasExited) {
          if (-not $readerOut.EndOfStream) { $o = $readerOut.ReadLine(); if ($o) { Write-Host ("[comfy] " + $o) } }
          if (-not $readerErr.EndOfStream) { $e = $readerErr.ReadLine(); if ($e) { Write-Host ("[comfy-err] " + $e) } }
          Start-Sleep -Milliseconds 100
        }
      } catch {}
    } -ArgumentList $proc | Out-Null
  } catch {
    Write-Log ("WARNING: failed to start background log job: " + $_.Exception.Message)
  }

  return $proc
}

function Test-TcpReady {
  param([string]$TcpHost, [int]$Port, [int]$TimeoutMs = 800)
  try {
    $client = New-Object System.Net.Sockets.TcpClient
    $iar = $client.BeginConnect($TcpHost, $Port, $null, $null)
    $ok = $iar.AsyncWaitHandle.WaitOne($TimeoutMs)
    if ($ok -and $client.Connected) {
      $client.Close()
      return $true
    } else {
      try { $client.Close() } catch {}
      return $false
    }
  } catch {
    return $false
  }
}

function Wait-HealthyTcp {
  param(
    [string]$TcpHost,
    [int]$Port,
    [int]$TimeoutSec,
    [int]$RetryDelaySec,
    [int]$InitialDelaySec
  )
  if ($InitialDelaySec -gt 0) { Start-Sleep -Seconds $InitialDelaySec }
  Write-Log ("TCP health-check on " + $TcpHost + ":" + $Port + " (timeout " + $TimeoutSec + "s, retry " + $RetryDelaySec + "s)")
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
    if (Test-TcpReady -TcpHost $TcpHost -Port $Port -TimeoutMs 800) {
      Write-Log ("TCP ready on " + $TcpHost + ":" + $Port)
      return $true
    }
    Start-Sleep -Seconds $RetryDelaySec
  }
  return $false
}

function Http-Confirm {
  # Minimal HTTP GET over raw TCP to avoid HttpClient/iwr dependencies.
  param([string]$TcpHost, [int]$Port, [string]$Path = "/")
  try {
    $client = New-Object System.Net.Sockets.TcpClient($TcpHost, $Port)
    $stream = $client.GetStream()
    $writer = New-Object System.IO.StreamWriter($stream)
    $writer.NewLine = "`r`n"
    $writer.AutoFlush = $true
    $request = "GET $Path HTTP/1.1`r`nHost: $TcpHost`r`nConnection: close`r`n`r`n"
    $writer.Write($request)
    $buffer = New-Object byte[] 512
    $read = $stream.Read($buffer, 0, $buffer.Length)
    $client.Close()
    if ($read -gt 0) {
      $header = [System.Text.Encoding]::ASCII.GetString($buffer, 0, [Math]::Min($read, 128))
      if ($header -match "^HTTP/\d\.\d\s+(\d{3})") {
        $code = [int]$Matches[1]
        return ($code -ge 200 -and $code -lt 500)
      }
    }
    return $false
  } catch {
    return $false
  }
}

# --------------- Main ---------------

$lockPath = $null
$lockCreated = $false
try {
  # Resolve and validate
  $MainPy = Resolve-MainPy -Root $ComfyRoot -Main $MainPy
  if (-not (Test-Path $ComfyRoot)) { Fail-And-Exit 1 ("ComfyRoot not found: " + $ComfyRoot) }
  if (-not (Test-Path $VenvPython)) { Fail-And-Exit 1 ("Venv Python not found: " + $VenvPython) }
  if (-not (Test-Path $MainPy)) { Fail-And-Exit 1 ("main.py not found: " + $MainPy) }

  # Acquire/remove stale lock with resilience
  $lockPath = Join-Path $ComfyRoot $LockFile
  if (Test-Path $lockPath) {
    Write-Log ("Lock file exists at " + $lockPath + ". Checking staleness...")
    $ageSec = [int]((Get-Date) - (Get-Item $lockPath).LastWriteTime).TotalSeconds
    if ($ForceUnlock) {
      Write-Log "FORCE_UNLOCK set → removing existing lock."
      Remove-Item $lockPath -ErrorAction SilentlyContinue
    } elseif ($ageSec -gt $ShortStaleSec) {
      Write-Log ("Lock age " + $ageSec + "s > " + $ShortStaleSec + "s → treating as stale. Removing.")
      Remove-Item $lockPath -ErrorAction SilentlyContinue
    } else {
      # brief debounce retry once
      Write-Log ("Lock is fresh (age " + $ageSec + "s ≤ " + $ShortStaleSec + "s). Waiting 2s and retrying once...")
      Start-Sleep -Seconds 2
      if (Test-Path $lockPath) {
        $ageSec2 = [int]((Get-Date) - (Get-Item $lockPath).LastWriteTime).TotalSeconds
        if ($ageSec2 -gt $ShortStaleSec) {
          Write-Log ("After wait, lock age " + $ageSec2 + "s > " + $ShortStaleSec + "s → removing.")
          Remove-Item $lockPath -ErrorAction SilentlyContinue
        } else {
          Fail-And-Exit 2 ("Another restart in progress (lock: " + $lockPath + ", age " + $ageSec2 + "s)")
        }
      }
    }
  }
  New-Item -ItemType File -Path $lockPath -Force | Out-Null
  $lockCreated = $true
  Write-Log ("Acquired lock: " + $lockPath)

  # Detect existing Comfy processes
  $existing = Get-ComfyProcesses -MainPyPath $MainPy
  if ($existing.Count -gt 0) {
    $procIds = ($existing | Select-Object -ExpandProperty ProcessId)
    if (-not (Stop-ComfyProcesses -ProcIds $procIds -TimeoutSec $KillTimeoutSec)) {
      Fail-And-Exit 3 "Failed to stop some ComfyUI processes."
    }
    $portFree = Wait-PortFree -p $Port -TimeoutSec $PortWaitTimeoutSec
    if (-not $portFree -and $AggressivePortFree) {
      Write-Log "Port still not free → killing explicit LISTEN/ESTABLISHED holders and retrying shortly."
      Kill-PortHolders -p $Port -Force
      Start-Sleep -Seconds 2
      $portFree = Test-PortFree -p $Port
    }
    if (-not $portFree) {
      Fail-And-Exit 4 ("Port " + $Port + " did not free within " + $PortWaitTimeoutSec + "s.")
    }
    Cleanup-SQLite-Journals -Root $ComfyRoot
    try {
      $proc = Start-Comfy -VenvPythonPath $VenvPython -WorkingDir $ComfyRoot -MainPyPath $MainPy -ListenAddr $Listen -ListenPort $Port -LowVRAM:$LowVRAM -DisableAutoLaunch:$DisableAutoLaunch -DatabaseUrl $DatabaseUrl
    } catch {
      Fail-And-Exit 5 ("Failed to start ComfyUI: " + $_.Exception.Message)
    }
  } else {
    Write-Log "No existing ComfyUI processes found."
    $portFree = Wait-PortFree -p $Port -TimeoutSec $PortWaitTimeoutSec
    if (-not $portFree -and $AggressivePortFree) {
      Write-Log "Port still not free → killing explicit LISTEN/ESTABLISHED holders and retrying shortly."
      Kill-PortHolders -p $Port -Force
      Start-Sleep -Seconds 2
      $portFree = Test-PortFree -p $Port
    }
    if (-not $portFree) {
      Fail-And-Exit 4 ("Port " + $Port + " did not free within " + $PortWaitTimeoutSec + "s.")
    }
    Cleanup-SQLite-Journals -Root $ComfyRoot
    try {
      $proc = Start-Comfy -VenvPythonPath $VenvPython -WorkingDir $ComfyRoot -MainPyPath $MainPy -ListenAddr $Listen -ListenPort $Port -LowVRAM:$LowVRAM -DisableAutoLaunch:$DisableAutoLaunch -DatabaseUrl $DatabaseUrl
    } catch {
      Fail-And-Exit 5 ("Failed to start ComfyUI: " + $_.Exception.Message)
    }
  }

  # TCP-based health
  $tcpHost = "127.0.0.1"
  $ok = Wait-HealthyTcp -TcpHost $tcpHost -Port $Port -TimeoutSec $HealthTimeoutSec -RetryDelaySec $HealthRetryDelaySec -InitialDelaySec $HealthInitialDelaySec
  if (-not $ok) {
    try { if ($proc -and -not $proc.HasExited) { $proc.Kill() | Out-Null } } catch {}
    Fail-And-Exit 6 ("TCP health-check failed for " + $tcpHost + ":" + $Port)
  }

  if ($HttpConfirm) {
    if (-not (Http-Confirm -TcpHost $tcpHost -Port $Port -Path "/")) {
      Write-Log "TCP ready but HTTP confirm did not return 200–499; proceeding anyway."
    } else {
      Write-Log "HTTP confirm succeeded."
    }
  }

  Write-Log ("ComfyUI is healthy (TCP) at " + $tcpHost + ":" + $Port)

  # Success
  if ($lockCreated -and (Test-Path $lockPath)) { Remove-Item $lockPath -ErrorAction SilentlyContinue }
  $lockCreated = $false
  exit 0

} catch {
  # Parse our Fail-And-Exit code if present
  $code = 7
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
