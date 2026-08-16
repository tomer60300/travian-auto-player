# Detects and clears a wedged Codex app-server broker.
#
# Failure mode this repairs: app-server-broker.mjs spawns `codex app-server` as a
# child and forwards every JSON-RPC request to it. If that child dies while the
# broker survives, the broker keeps listening on its named pipe, so
# isBrokerEndpointReady() (which only tests connectability) happily reuses it --
# but AppServerClientBase.request() never rejects once handleExit() has already
# run, so the forwarded request never settles. Reviews then hang forever with
# zero output and no error.
#
# Safe to run any time: it only kills brokers that have no live app-server.

$ErrorActionPreference = 'Stop'

$brokers = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -eq 'node.exe' -and $_.CommandLine -like '*app-server-broker.mjs*' }

if (-not $brokers) {
    Write-Output 'No broker running. Nothing to clear.'
} else {
    foreach ($b in $brokers) {
        $kids = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($b.ProcessId)")
        if ($kids.Count -gt 0) {
            Write-Output "Broker $($b.ProcessId) is healthy ($($kids.Count) child process(es)). Left alone."
        } else {
            Write-Output "Broker $($b.ProcessId) is WEDGED (no app-server child). Killing."
            Stop-Process -Id $b.ProcessId -Force
        }
    }
}

# Drop stale broker.json state so the next run spawns a fresh broker + pipe.
Get-ChildItem "$env:USERPROFILE\.claude\plugins\data\codex-openai-codex\state" `
    -Recurse -Filter 'broker.json' -ErrorAction SilentlyContinue | ForEach-Object {
    $pidVal = (Get-Content $_.FullName -Raw | ConvertFrom-Json).pid
    if (-not (Get-Process -Id $pidVal -ErrorAction SilentlyContinue)) {
        Write-Output "Removing stale state for dead broker PID $pidVal : $($_.FullName)"
        Remove-Item $_.FullName -Force
    }
}

Write-Output 'Done.'
