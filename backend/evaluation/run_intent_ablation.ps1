param(
    [Parameter(Mandatory = $true)][string]$Image,
    [ValidateRange(1, 20)][int]$Rounds = 5,
    [string]$Python = 'python',
    [switch]$Smoke
)

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$stamp = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
$root = "data/intent-ablation/$stamp"
$null = New-Item -ItemType Directory -Path $root
$override = "$root/compose.override.yml"
$imageId = docker image inspect --format '{{.Id}}' $Image
if ($LASTEXITCODE -ne 0) { throw 'The isolated API image must exist locally.' }
if ($Image -notmatch '^[a-zA-Z0-9.\-_:]+$') { throw 'Use a local image tag without a registry prefix.' }
@"
services:
  redis:
    healthcheck:
      interval: 2s
  chromadb:
    healthcheck:
      interval: 2s
  dian:
    image: "$Image"
    environment:
      DIAN_TICKET_PROVIDER: sqlite
      DIAN_TEMPORAL_LOGISTICS: "false"
      INTENT_EMBEDDING_ENABLED: "true"
      ALERT_WEBHOOK_URL: ""
      PROMETHEUS_PORT: "0"
    healthcheck:
      interval: 2s
"@ | Set-Content -LiteralPath $override -Encoding utf8

$groups = @('rules_only', 'llm_rules', 'full', 'full_off')
$ledger = "$root/schedule.jsonl"
Write-Output "Replay output: $root"

for ($round = 1; $round -le $Rounds; $round++) {
    # Rotate the order so a mode does not always run first or last.
    $order = @(0..3 | ForEach-Object { $groups[($_ + $round - 1) % 4] })
    if ($Smoke) { $order = @('llm_rules') }
    foreach ($group in $order) {
        $project = "dian-a1-$stamp-$round-" + $group.Replace('_', '-')
        $groupDir = "$root/r$($round.ToString('00'))/$group"
        $null = New-Item -ItemType Directory -Path "$groupDir/eval", "$groupDir/logistics"
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
        $listener.Start()
        $port = $listener.LocalEndpoint.Port
        $listener.Stop()
        $env:DIAN_ACCEPTANCE_PORT = "$port"
        $env:DIAN_ACCEPTANCE_DATA_DIR = "./$groupDir"
        $env:DIAN_INTENT_MODE = if ($group -eq 'full_off') { 'full' } else { $group }
        $env:DIAN_LOGISTICS_EMBEDDING_ENABLED = if ($group -eq 'full_off') { 'false' } else { 'true' }
        $compose = @('compose', '-p', $project, '-f', 'docker-compose.acceptance.yml',
                     '-f', 'docker-compose.local-check.yml', '-f', $override)
        $started = [DateTimeOffset]::UtcNow.ToString('o')
        try {
            docker @compose up -d --no-build --pull never --wait --wait-timeout 90
            if ($LASTEXITCODE -ne 0) { throw "Could not start the isolated replay stack for $group." }
            $container = docker @compose ps -q dian
            if ($LASTEXITCODE -ne 0 -or -not $container) { throw 'Target API container was not found.' }
            $actualImage = docker inspect --format '{{.Image}}' $container
            if ($actualImage -ne $imageId) { throw 'Target image does not match the session image.' }
            $argsForReplay = @('-m', 'evaluation.logistics_eval_runner', '--split', 'dev',
                '--variant', $env:DIAN_INTENT_MODE, '--base-url', "http://127.0.0.1:$port",
                '--db', "$groupDir/logistics/cases.sqlite3", '--container', $container,
                '--restart-command', "docker $($compose -join ' ') restart dian",
                '--output', "$groupDir/eval/report.json")
            if ($Smoke) { $argsForReplay += @('--case-id', 'DIAN-029') }
            & $Python @argsForReplay
            $rc = $LASTEXITCODE
            if (-not (Test-Path -LiteralPath "$groupDir/eval/report.json")) { throw 'Replay produced no report.' }
            $raw = Get-Content -LiteralPath "$groupDir/eval/report.json" -Raw | ConvertFrom-Json
            [ordered]@{
                round = $round
                group = $group
                started_utc = $started
                finished_utc = [DateTimeOffset]::UtcNow.ToString('o')
                exit_code = $rc
                summary = $raw.summary
            } | ConvertTo-Json -Depth 4 -Compress | Add-Content -LiteralPath $ledger -Encoding utf8
            Write-Output "Round $round, $group, exit $rc"
            if ($raw.provenance.verification_errors.Count -gt 0) {
                throw 'Target identity changed during the replay. Existing reports were kept.'
            }
            if ($Smoke -and $rc -ne 0) { throw 'Model smoke replay failed. Inspect its report before the full run.' }
        } finally {
            # This project name is generated for this one run.
            docker @compose down --volumes
        }
    }
    if ($Smoke) { break }
}

if (-not $Smoke) {
    foreach ($contrast in @('intent_mode', 'logistics_embedding')) {
        & $Python -m evaluation.compare_logistics_ablation --root $root --rounds $Rounds --contrast $contrast --output "$root/$contrast.json"
        if ($LASTEXITCODE -ne 0) { throw "Reports are not comparable for $contrast." }
    }
}
