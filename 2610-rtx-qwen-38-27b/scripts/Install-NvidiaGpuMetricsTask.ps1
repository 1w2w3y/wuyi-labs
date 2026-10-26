<#
.SYNOPSIS
Runs Export-NvidiaGpuMetrics.ps1 in the background as a scheduled task for the
signed-in user.

.DESCRIPTION
The task starts at logon, without a console window, and a five-minute trigger
restarts the collector if it has stopped. It needs no elevation.

.EXAMPLE
.\Install-NvidiaGpuMetricsTask.ps1                  # install and start
.\Install-NvidiaGpuMetricsTask.ps1 -Operation Status
.\Install-NvidiaGpuMetricsTask.ps1 -Operation Uninstall
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet('Install', 'Status', 'Uninstall')]
    [string]$Operation = 'Install',
    [string]$TaskName = 'LocalLlm-NvidiaGpuMetrics',
    [string]$OutputDirectory = 'C:\ProgramData\local-llm\textfile',
    [ValidateRange(1, 3600)]
    [int]$IntervalSeconds = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$collector = Join-Path $PSScriptRoot 'Export-NvidiaGpuMetrics.ps1'
$launcher = Join-Path $PSScriptRoot 'Run-Hidden.vbs'
$metricFile = Join-Path $OutputDirectory 'nvidia_gpu.prom'

function Stop-Collector {
    # Run-Hidden.vbs starts PowerShell as a child process, which stopping the
    # task doesn't end, so stop the collector process itself too.
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($collector, [StringComparison]::OrdinalIgnoreCase) -ge 0 } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

switch ($Operation) {
    'Status' {
        if (-not $existing) { throw "Scheduled task '$TaskName' is not installed." }
        $age = $null
        if (Test-Path -LiteralPath $metricFile) {
            $age = [Math]::Round(((Get-Date) - (Get-Item -LiteralPath $metricFile).LastWriteTime).TotalSeconds, 1)
        }
        return [pscustomobject]@{
            Task          = $TaskName
            State         = $existing.State
            LastRunResult = (Get-ScheduledTaskInfo -TaskName $TaskName).LastTaskResult
            MetricFile    = $metricFile
            AgeSeconds    = $age
        }
    }

    'Uninstall' {
        if ($existing -and $PSCmdlet.ShouldProcess($TaskName, 'Stop and unregister the scheduled task')) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Stop-Collector
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        return
    }
}

foreach ($path in @($collector, $launcher)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "'$path' was not found." }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$powershell = (Get-Command powershell.exe).Source
$arguments = @(
    '//B', '//NoLogo', ('"{0}"' -f $launcher),
    ('"{0}"' -f $powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $collector), '-Loop',
    '-IntervalSeconds', $IntervalSeconds,
    '-OutputDirectory', ('"{0}"' -f $OutputDirectory)
) -join ' '

$action = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\wscript.exe') -Argument $arguments
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $identity),
    # A watchdog: IgnoreNew leaves a running collector alone and starts a stopped one.
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 5))
)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable
$task = New-ScheduledTask -Action $action -Trigger $triggers -Principal $principal -Settings $settings `
    -Description "Writes NVIDIA GPU metrics for windows_exporter every $IntervalSeconds seconds."

if (-not $PSCmdlet.ShouldProcess($TaskName, "Register and start a scheduled task that writes '$metricFile'")) {
    return
}

[IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Stop-Collector
}
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

# Wait for the first fresh file, so a broken setup fails here and not on the dashboard.
$deadline = (Get-Date).AddSeconds([Math]::Max(20, $IntervalSeconds * 4))
do {
    Start-Sleep -Seconds 1
    $fresh = (Test-Path -LiteralPath $metricFile) -and
        ((Get-Date) - (Get-Item -LiteralPath $metricFile).LastWriteTime).TotalSeconds -lt ($IntervalSeconds * 3)
} until ($fresh -or (Get-Date) -gt $deadline)
if (-not $fresh) {
    throw "The collector did not write '$metricFile'. Run Export-NvidiaGpuMetrics.ps1 by hand to see the error."
}

Get-Content -LiteralPath $metricFile | Where-Object { $_ -match '^nvidia_gpu_(exporter_up|power_draw_watts|memory_total_bytes)' }
