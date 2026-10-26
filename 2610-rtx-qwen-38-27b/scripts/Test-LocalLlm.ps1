<#
.SYNOPSIS
Checks the model through the LiteLLM gateway the way a coding agent uses it:
an exact answer, an image, a required tool call, and the metrics endpoints the
dashboard reads.

.EXAMPLE
.\Test-LocalLlm.ps1
.\Test-LocalLlm.ps1 -BaseUrl http://<laptop-address>:4000/v1
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:4000/v1',
    [string]$Model = 'local-llm',
    [string]$VllmMetricsUrl = 'http://127.0.0.1:8096/metrics',
    [string]$WindowsExporterUrl = 'http://127.0.0.1:9182/metrics'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$failures = 0

function Write-Check {
    param([string]$Name, [bool]$Passed, [string]$Detail = '')
    $status = if ($Passed) { 'PASS' } else { 'FAIL' }
    if (-not $Passed) { $script:failures++ }
    Write-Host ("{0} {1}{2}" -f $status, $Name, $(if ($Detail) { " ($Detail)" } else { '' }))
}

function Invoke-Chat {
    param([hashtable]$Body)
    $Body.model = $Model
    $Body.temperature = 0
    # Thinking off keeps the checks fast and the answers exact.
    $Body.chat_template_kwargs = @{ enable_thinking = $false }
    $json = $Body | ConvertTo-Json -Depth 12 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    return Invoke-RestMethod -Method Post -Uri "$BaseUrl/chat/completions" -ContentType 'application/json' -Body $bytes -TimeoutSec 300
}

function Test-Step {
    param([string]$Name, [scriptblock]$Script)
    try { & $Script }
    catch { Write-Check $Name $false $_.Exception.Message }
}

Test-Step 'gateway lists the model' {
    $models = Invoke-RestMethod -Uri "$BaseUrl/models" -TimeoutSec 30
    Write-Check 'gateway lists the model' (@($models.data.id) -contains $Model)
}

Test-Step 'exact answer' {
    $reply = Invoke-Chat @{ max_tokens = 16; messages = @(@{ role = 'user'; content = 'Reply with exactly: PONG' }) }
    $text = ([string]$reply.choices[0].message.content).Trim()
    Write-Check 'exact answer' ($text -eq 'PONG') "got '$text'"
}

Test-Step 'reads an image' {
    # A 64-by-64 solid red PNG, drawn here so the test needs no files.
    Add-Type -AssemblyName System.Drawing
    $bitmap = New-Object Drawing.Bitmap 64, 64
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    $graphics.Clear([Drawing.Color]::Red)
    $stream = New-Object IO.MemoryStream
    $bitmap.Save($stream, [Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose(); $bitmap.Dispose()
    $dataUrl = 'data:image/png;base64,' + [Convert]::ToBase64String($stream.ToArray())

    $reply = Invoke-Chat @{
        max_tokens = 16
        messages   = @(@{
                role    = 'user'
                content = @(
                    @{ type = 'text'; text = 'What color is this image? Answer with one word.' },
                    @{ type = 'image_url'; image_url = @{ url = $dataUrl } }
                )
            })
    }
    $text = ([string]$reply.choices[0].message.content).Trim()
    Write-Check 'reads an image' ($text -match '(?i)\bred\b') "got '$text'"
}

Test-Step 'required tool call' {
    $reply = Invoke-Chat @{
        max_tokens  = 512
        messages    = @(@{ role = 'user'; content = 'Read the file README.md using the tool.' })
        tools       = @(@{
                type     = 'function'
                function = @{
                    name        = 'read_file'
                    description = 'Read a file'
                    parameters  = @{ type = 'object'; properties = @{ path = @{ type = 'string' } }; required = @('path') }
                }
            })
        tool_choice = 'required'
    }
    $call = @($reply.choices[0].message.tool_calls)[0]
    Write-Check 'required tool call' ($call.function.name -eq 'read_file') "arguments $($call.function.arguments)"
}

Test-Step 'metrics endpoints' {
    $vllm = (Invoke-WebRequest -UseBasicParsing -Uri $VllmMetricsUrl -TimeoutSec 30).Content
    Write-Check 'vLLM metrics include speculative decoding' ($vllm -match '(?m)^vllm:spec_decode_num_drafts_total')
    $windows = (Invoke-WebRequest -UseBasicParsing -Uri $WindowsExporterUrl -TimeoutSec 30).Content
    Write-Check 'windows_exporter serves GPU metrics' ($windows -match '(?m)^nvidia_gpu_power_draw_watts\{')
    Write-Check 'windows_exporter serves shared GPU memory' ($windows -match '(?m)^windows_gpu_adapter_memory_shared_bytes\{')
}

if ($failures -eq 0) { Write-Host 'ALL PASS' } else { Write-Host "$failures CHECK(S) FAILED"; exit 1 }
