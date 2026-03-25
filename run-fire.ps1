# run-fire.ps1 - Launch a script elevated, fire-and-forget. Poll the log file for progress.
param(
    [Parameter(Mandatory=$true)][string]$Script
)
Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -File `"$Script`"" -Verb RunAs -WindowStyle Hidden
Write-Output "Launched elevated. Poll log file for progress."
