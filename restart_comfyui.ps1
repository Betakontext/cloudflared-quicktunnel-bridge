# restart_comfyui.ps1
# Purpose: Restart ComfyUI on Windows with default args but customizable.
# Adjust paths as needed.

$ErrorActionPreference = "Stop"

# 1) Try to stop existing ComfyUI python process (best-effort)
#    Use your own detection: process name, window title, or a PID file you maintain.
#    Here we look for a python.exe running main.py in ComfyUI folder.
$comfyRoot = "C:\Users\Administrator\Documents\Arbeiten\0000_DEV\ComfyUI"
$pythonExe = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"  # adjust to your env
$mainPy    = Join-Path $comfyRoot "main.py"

Write-Host "[comfy-restart] stopping existing ComfyUI if running..."
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like "*$mainPy*"
} | ForEach-Object {
    try {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 300
    } catch {}
}

# 2) Start ComfyUI with your defaults (customize as required)
#    Example defaults you mentioned:
#    python main.py --listen 0.0.0.0 --port 8188 --lowvram
#    You can add/remove args as needed; consider a .args file to externalize.
$arguments = @(
    "`"$mainPy`"",
    "--listen", "0.0.0.0",
    "--port", "8188",
    "--lowvram"
)

Write-Host "[comfy-restart] starting ComfyUI..."
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $pythonExe
$startInfo.WorkingDirectory = $comfyRoot
$startInfo.Arguments = $arguments -join " "
$startInfo.UseShellExecute = $false
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$proc = [System.Diagnostics.Process]::Start($startInfo)

if ($proc -ne $null) {
    Write-Host "[comfy-restart] ComfyUI started. PID=$($proc.Id)"
} else {
    Write-Error "[comfy-restart] failed to start ComfyUI"
    exit 1
}
