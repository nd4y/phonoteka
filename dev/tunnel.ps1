param([ValidateSet('start','status','stop')][string]$Action = 'start',
      [ValidateRange(1024,65535)][int]$LocalPort = 8177,
      [ValidateRange(1024,65535)][int]$RemotePort = 8177,
      [ValidateSet('127.0.0.1','localhost')][string]$DisplayHost = '127.0.0.1')
$ErrorActionPreference = 'Stop'
$previewUrl = "http://${DisplayHost}:$LocalPort"
$recordSuffix = if ($LocalPort -eq 8177 -and $RemotePort -eq 8177) { '' } else { "-$LocalPort-$RemotePort" }
$tunnelRecord = Join-Path $env:TEMP "phonoteka-build01-tunnel$recordSuffix.json"
$tunnelError = Join-Path $env:TEMP "phonoteka-build01-tunnel$recordSuffix.stderr.log"
$tunnelOutput = Join-Path $env:TEMP "phonoteka-build01-tunnel$recordSuffix.stdout.log"
$forwardSpec = "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}"
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
    [pscustomobject]@{ active = [bool]$savedProcess; pid = $savedProcess.ProcessId; url = $previewUrl }
    return
}
if ($savedProcess) { Write-Output "Project tunnel already running: $previewUrl"; return }
if (Get-NetTCPConnection -State Listen -LocalPort $LocalPort -ErrorAction SilentlyContinue) {
    throw "Local port $LocalPort is occupied. Its process was not stopped."
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
    $listener = Get-NetTCPConnection -State Listen -LocalPort $LocalPort -ErrorAction SilentlyContinue
    if ($listener -and $listener.OwningProcess -contains $child.Id) {
        Write-Output "Tunnel ready: $previewUrl (SSH PID $($child.Id))"
        return
    }
    Start-Sleep -Milliseconds 250
}
throw "SSH tunnel did not open the listener; inspect $tunnelError"
