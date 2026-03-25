# run-elevated.ps1 — Run a command elevated and capture output
# Usage: powershell -ExecutionPolicy Bypass -File run-elevated.ps1 -Cmd "docker ps"
param(
    [Parameter(Mandatory=$true)][string]$Cmd,
    [string]$OutFile = "$env:TEMP\openclaw-elevated-out.txt"
)

# If already admin, just run it
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($isAdmin) {
    $result = cmd /c "$Cmd" 2>&1
    $result | Out-File -FilePath $OutFile -Encoding UTF8
    $result
    exit $LASTEXITCODE
}

# Not admin — relaunch self elevated
$selfPath = $MyInvocation.MyCommand.Path
$argList = "-ExecutionPolicy Bypass -File `"$selfPath`" -Cmd `"$Cmd`" -OutFile `"$OutFile`""

Remove-Item $OutFile -Force -ErrorAction SilentlyContinue

$proc = Start-Process powershell -ArgumentList $argList -Verb RunAs -PassThru -WindowStyle Hidden
$proc.WaitForExit(120000) | Out-Null

if (Test-Path $OutFile) {
    Get-Content $OutFile
} else {
    Write-Error "No output captured - UAC may have been denied"
}
