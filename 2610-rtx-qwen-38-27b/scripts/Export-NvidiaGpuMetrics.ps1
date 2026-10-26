<#
.SYNOPSIS
Writes NVIDIA GPU power, temperature, clocks, utilization and memory as a
Prometheus textfile for windows_exporter.

.DESCRIPTION
DCGM, which data-center GPU monitoring usually relies on, isn't available on
Windows, so this script polls nvidia-smi instead. windows_exporter's textfile
collector serves the file it writes. Run it once to test, or with -Loop from
the scheduled task that Install-NvidiaGpuMetricsTask.ps1 registers.

.EXAMPLE
.\Export-NvidiaGpuMetrics.ps1
Get-Content C:\ProgramData\local-llm\textfile\nvidia_gpu.prom
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory = 'C:\ProgramData\local-llm\textfile',
    [ValidateRange(1, 3600)]
    [int]$IntervalSeconds = 5,
    [switch]$Loop,
    [string]$NvidiaSmiPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Find-NvidiaSmi {
    if ($NvidiaSmiPath) {
        if (-not (Test-Path -LiteralPath $NvidiaSmiPath -PathType Leaf)) {
            throw "nvidia-smi was not found at '$NvidiaSmiPath'."
        }
        return (Get-Item -LiteralPath $NvidiaSmiPath).FullName
    }

    $command = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    foreach ($path in @(
            (Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'),
            (Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVSMI\nvidia-smi.exe'))) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            return $path
        }
    }

    throw 'nvidia-smi.exe was not found in PATH or a standard NVIDIA installation directory.'
}

function ConvertTo-NumberOrNull {
    param([AllowNull()][string]$Value)

    $parsed = 0.0
    $style = [Globalization.NumberStyles]::Float
    $culture = [Globalization.CultureInfo]::InvariantCulture
    if (-not [string]::IsNullOrWhiteSpace($Value) -and [double]::TryParse($Value.Trim(), $style, $culture, [ref]$parsed)) {
        return $parsed
    }
    # nvidia-smi prints "[N/A]" for fields a GPU doesn't report.
    return $null
}

function Format-PrometheusNumber {
    param([double]$Value)
    return $Value.ToString('0.################', [Globalization.CultureInfo]::InvariantCulture)
}

function Write-FileAtomically {
    param(
        [Parameter(Mandatory)][string]$Content,
        [Parameter(Mandatory)][string]$Destination
    )

    # windows_exporter may read the file at any moment, so write a temporary
    # file and swap it in rather than rewriting the file in place.
    $directory = Split-Path -Parent $Destination
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    $leaf = Split-Path -Leaf $Destination
    $temporary = Join-Path $directory (".$leaf.$PID.tmp")
    $backup = Join-Path $directory (".$leaf.$PID.bak")
    try {
        [IO.File]::WriteAllText($temporary, $Content, (New-Object Text.UTF8Encoding $false))
        if ([IO.File]::Exists($Destination)) {
            # Windows PowerShell 5.1 rejects a null backup path for File.Replace.
            [IO.File]::Replace($temporary, $Destination, $backup, $true)
        }
        else {
            [IO.File]::Move($temporary, $Destination)
        }
    }
    finally {
        foreach ($path in @($temporary, $backup)) {
            if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) }
        }
    }
}

function Get-NvidiaGpuMetrics {
    param([Parameter(Mandatory)][string]$Executable)

    $query = 'index,name,utilization.gpu,memory.total,memory.used,temperature.gpu,power.draw,clocks.gr'
    $output = @(& $Executable "--query-gpu=$query" '--format=csv,noheader,nounits' 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "nvidia-smi exited with code ${LASTEXITCODE}: $($output -join ' ')"
    }

    $headers = 'Index', 'Name', 'Utilization', 'MemoryTotalMiB', 'MemoryUsedMiB', 'Temperature', 'PowerWatts', 'GraphicsClockMHz'
    # On RTX Spark, nvidia-smi also lists the NPU, with a temperature but no
    # memory. Keep only the GPU, so max() over these series means the GPU.
    $rows = @($output | ForEach-Object { [string]$_ } | Where-Object { $_.Trim() } | ConvertFrom-Csv -Header $headers |
        Where-Object { $_.Name -notmatch '(?i)\bNPU\b' -and $null -ne (ConvertTo-NumberOrNull $_.MemoryTotalMiB) })
    if ($rows.Count -eq 0) {
        throw 'nvidia-smi returned no GPUs.'
    }

    $metrics = [ordered]@{
        nvidia_gpu_utilization_ratio    = @('gauge', 'Fraction of time a GPU kernel was executing.', 'Utilization', 0.01)
        nvidia_gpu_memory_total_bytes   = @('gauge', 'GPU memory: on RTX Spark, the share of unified memory set aside for the GPU.', 'MemoryTotalMiB', 1048576)
        nvidia_gpu_memory_used_bytes    = @('gauge', 'GPU memory in use.', 'MemoryUsedMiB', 1048576)
        nvidia_gpu_temperature_celsius  = @('gauge', 'GPU temperature.', 'Temperature', 1)
        nvidia_gpu_power_draw_watts     = @('gauge', 'GPU power draw.', 'PowerWatts', 1)
        nvidia_gpu_graphics_clock_hertz = @('gauge', 'Graphics clock.', 'GraphicsClockMHz', 1000000)
    }

    $lines = New-Object 'Collections.Generic.List[string]'
    $lines.Add('# HELP nvidia_gpu_exporter_up Whether the last nvidia-smi collection succeeded.')
    $lines.Add('# TYPE nvidia_gpu_exporter_up gauge')
    $lines.Add('nvidia_gpu_exporter_up 1')
    $lines.Add('# HELP nvidia_gpu_exporter_last_success_unixtime_seconds Unix time of the last successful collection.')
    $lines.Add('# TYPE nvidia_gpu_exporter_last_success_unixtime_seconds gauge')
    $lines.Add("nvidia_gpu_exporter_last_success_unixtime_seconds $([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())")

    foreach ($name in $metrics.Keys) {
        $type, $help, $field, $multiplier = $metrics[$name]
        $lines.Add("# HELP $name $help")
        $lines.Add("# TYPE $name $type")
        foreach ($row in $rows) {
            $value = ConvertTo-NumberOrNull $row.$field
            if ($null -ne $value) {
                $gpuName = ([string]$row.Name).Trim().Replace('\', '\\').Replace('"', '\"')
                $labels = 'gpu="{0}",name="{1}"' -f ([string]$row.Index).Trim(), $gpuName
                $lines.Add("$name{$labels} $(Format-PrometheusNumber ($value * $multiplier))")
            }
        }
    }

    # windows_exporter requires a textfile to end with a newline.
    return ($lines -join "`n") + "`n"
}

$destination = Join-Path ([IO.Path]::GetFullPath($OutputDirectory)) 'nvidia_gpu.prom'
$nvidiaSmi = Find-NvidiaSmi

do {
    $started = [Diagnostics.Stopwatch]::StartNew()
    try {
        Write-FileAtomically -Content (Get-NvidiaGpuMetrics -Executable $nvidiaSmi) -Destination $destination
    }
    catch {
        # Publish the failure so a dashboard or alert sees it, then keep looping.
        Write-FileAtomically -Destination $destination -Content (
            "# HELP nvidia_gpu_exporter_up Whether the last nvidia-smi collection succeeded.`n" +
            "# TYPE nvidia_gpu_exporter_up gauge`nnvidia_gpu_exporter_up 0`n")
        if (-not $Loop) { throw }
        Write-Warning "nvidia-smi collection failed; retrying: $($_.Exception.Message)"
    }

    if ($Loop) {
        $remaining = $IntervalSeconds * 1000 - $started.ElapsedMilliseconds
        if ($remaining -gt 0) { Start-Sleep -Milliseconds $remaining }
    }
} while ($Loop)
