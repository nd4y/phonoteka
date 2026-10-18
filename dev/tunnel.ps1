param([ValidateSet('start','status','stop')][string]$Action = 'start')
$ErrorActionPreference = 'Stop'
$tunnelRecord = Join-Path $env:TEMP 'phonoteka-build01-tunnel.json'
$tunnelError = Join-Path $env:TEMP 'phonoteka-build01-tunnel.stderr.log'
$tunnelOutput = Join-Path $env:TEMP 'phonoteka-build01-tunnel.stdout.log'
$forwardSpec = '127.0.0.1:8177:127.0.0.1:8177'
$destination = 'av@192.168.0.21'
$savedProcess = $null
if (Test-Path -LiteralPath $tunnelRecord) {
    $saved = Get-Content -Raw -LiteralPath $tunnelRecord | ConvertFrom-Json
    $candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$($saved.pid)"
    if ($candidate -and $candidate.Name -eq 'ssh.exe' -and
        $candidate.CommandLine.Contains($forwardSpec) -and $candidate.CommandLine.Contains($destination) -and
        $candidate.CreationDate.ToUniversalTime().Ticks -eq $saved.createdTicks) {
        $savedProcess = $candidate
    }
}
if ($Action -eq 'stop') {
    if ($savedProcess) { Stop-Process -Id $savedProcess.ProcessId }
    Remove-Item -LiteralPath $tunnelRecord -ErrorAction SilentlyContinue
    Write-Output 'Project tunnel stopped; other SSH processes unchanged.'
    return
}
if ($Action -eq 'status') {
    [pscustomobject]@{ active = [bool]$savedProcess; pid = $savedProcess.ProcessId; url = 'http://127.0.0.1:8177' }
    return
}
if ($savedProcess) { Write-Output 'Project tunnel already running.'; return }
if (Get-NetTCPConnection -State Listen -LocalPort 8177 -ErrorAction SilentlyContinue) {
    throw 'Local port 8177 is occupied. Its process was not stopped.'
}
$sshExecutable = (Get-Command ssh.exe).Source
$child = Start-Process -FilePath $sshExecutable -ArgumentList @(
    '-T','-N','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes',
    '-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
    '-L',$forwardSpec,$destination
) -WindowStyle Hidden -PassThru -RedirectStandardError $tunnelError -RedirectStandardOutput $tunnelOutput
$candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$($child.Id)"
if (-not $candidate) { throw "SSH failed to start; inspect $tunnelError" }
@{pid=$child.Id;createdTicks=$candidate.CreationDate.ToUniversalTime().Ticks} |
    ConvertTo-Json | Set-Content -LiteralPath $tunnelRecord -Encoding utf8
for ($attempt=0; $attempt -lt 20; $attempt++) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort 8177 -ErrorAction SilentlyContinue
    if ($listener -and $listener.OwningProcess -contains $child.Id) {
        Write-Output "Tunnel ready: http://127.0.0.1:8177 (SSH PID $($child.Id))"
        return
    }
    Start-Sleep -Milliseconds 250
}
throw "SSH tunnel did not open the listener; inspect $tunnelError"
