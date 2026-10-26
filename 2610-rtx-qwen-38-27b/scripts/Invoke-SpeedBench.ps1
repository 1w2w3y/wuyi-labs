<#
.SYNOPSIS
Measures single-request decode and prefill speed inside the model container,
as in the blog post's code and long-prompt benchmarks.

.DESCRIPTION
Copies speed_bench.py and a text corpus into the qwen3.8-27b container and runs
it there against vLLM directly, one request at a time: three short
code-generation prompts (512 tokens, temperature 0, thinking off), two
thinking prompts, and long prompts cut from the corpus with a random first
line so that none of them come from the prefix cache. It also records tokens
per decode step from vLLM's speculative-decoding counters.

The corpus is the text files of a Git repository, by default the one this
sample is in. The post's long-prompt numbers came from a different repository,
so expect different prompts, though decode speed should be close.

.EXAMPLE
.\Invoke-SpeedBench.ps1 -Label baseline
.\Invoke-SpeedBench.ps1 -Label long-only -Only long -LongTokens 100000
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Label,
    [string]$Container = 'qwen3.8-27b',
    [string]$CorpusRepository,
    [string]$LongTokens = '32000,100000',
    [ValidateSet('code', 'think', 'long')]
    [string[]]$Only,
    [string]$ResultsDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$sampleRoot = Split-Path -Parent $PSScriptRoot

if (-not $CorpusRepository) {
    $CorpusRepository = (git -C $sampleRoot rev-parse --show-toplevel).Trim()
}
if (-not $ResultsDirectory) {
    $ResultsDirectory = Join-Path $sampleRoot 'results'
}
New-Item -ItemType Directory -Force -Path $ResultsDirectory | Out-Null

# Concatenate the repository's text files in a stable order, so that repeated
# runs see the same tokens.
$files = git -C $CorpusRepository ls-files |
    Where-Object { $_ -match '\.(md|ps1|py|sh|ya?ml|json|cs|ts|go|txt)$' } |
    Sort-Object
$builder = New-Object Text.StringBuilder
foreach ($file in $files) {
    $text = [IO.File]::ReadAllText((Join-Path $CorpusRepository $file))
    [void]$builder.Append("===== $file =====`n").Append($text).Append("`n`n")
}
$corpusPath = Join-Path ([IO.Path]::GetTempPath()) "speed-bench-corpus-$PID.txt"
[IO.File]::WriteAllText($corpusPath, $builder.ToString(), (New-Object Text.UTF8Encoding $false))
$approxTokens = [int]($builder.Length / 3.2)
$largest = ($LongTokens -split ',' | ForEach-Object { [int]$_ } | Measure-Object -Maximum).Maximum
if ($approxTokens -lt $largest) {
    Write-Warning "The corpus has about $approxTokens tokens, fewer than the $largest-token long prompt; that prompt will be shorter."
}

try {
    docker cp (Join-Path $PSScriptRoot 'speed_bench.py') "${Container}:/tmp/speed_bench.py"
    if ($LASTEXITCODE -ne 0) { throw "Copying the bench into '$Container' failed. Is the model running?" }
    docker cp $corpusPath "${Container}:/tmp/speed_bench_corpus.txt"
    if ($LASTEXITCODE -ne 0) { throw 'Copying the corpus failed.' }

    $benchArgs = @('python3', '/tmp/speed_bench.py', '--label', $Label,
        '--corpus', '/tmp/speed_bench_corpus.txt', '--long-tokens', $LongTokens)
    if ($Only) { $benchArgs += @('--only', ($Only -join ',')) }

    # The bench prints progress on stderr and the results as JSON on stdout.
    # Windows PowerShell 5.1 turns native stderr into error records, so take
    # both streams and split them here.
    $ErrorActionPreference = 'Continue'
    $output = docker exec $Container @benchArgs 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    $output | Where-Object { $_ -is [Management.Automation.ErrorRecord] } | ForEach-Object { Write-Host $_.ToString() }
    if ($exitCode -ne 0) { throw "The bench exited with code $exitCode." }

    $json = ($output | Where-Object { $_ -isnot [Management.Automation.ErrorRecord] }) -join "`n"
    $outPath = Join-Path $ResultsDirectory ('{0}-{1}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $Label)
    [IO.File]::WriteAllText($outPath, $json, (New-Object Text.UTF8Encoding $false))

    $rows = $json | ConvertFrom-Json
    $rows | Select-Object id, prompt_tokens, completion_tokens, ttft_s, decode_tps, prefill_tps, acceptance_length |
        Format-Table -AutoSize | Out-String | Write-Host
    $code = @($rows | Where-Object { $_.kind -eq 'code' -and $_.decode_tps })
    if ($code.Count) {
        Write-Host ('Code benchmark: {0:N1} tok/s decode, mean of {1}' -f ($code | Measure-Object decode_tps -Average).Average, $code.Count)
    }
    Write-Host "Wrote $outPath"
}
finally {
    Remove-Item -LiteralPath $corpusPath -Force -ErrorAction SilentlyContinue
}
