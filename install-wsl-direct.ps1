$log = "$env:TEMP\openclaw-wsl-direct.log"
"" | Out-File $log -Encoding UTF8
function Log($m) { $l="[$(Get-Date -f 'HH:mm:ss')] $m"; Write-Output $l; $l | Out-File -Append $log -Encoding UTF8 }

Log "Downloading Ubuntu 22.04 WSL image..."
$url = "https://aka.ms/wslubuntu2204"
$dest = "$env:TEMP\Ubuntu2204.appx"
if (-not (Test-Path $dest)) {
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}
Log "Downloaded: $(Get-Item $dest | Select-Object -ExpandProperty Length) bytes"

Log "Installing via Add-AppxPackage..."
Add-AppxPackage -Path $dest 2>&1 | ForEach-Object { Log $_ }

Log "Checking WSL list..."
wsl --list --verbose 2>&1 | ForEach-Object { Log $_ }

Log "Initializing Ubuntu with root user..."
ubuntu2204.exe install --root 2>&1 | ForEach-Object { Log $_ }

Log "Verifying..."
wsl -d Ubuntu-22.04 -u root -- echo "WSL Ubuntu OK" 2>&1 | ForEach-Object { Log $_ }

Log "=== DONE ==="
