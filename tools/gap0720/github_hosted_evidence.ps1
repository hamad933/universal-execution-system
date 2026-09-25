param(
  [string]$EvidenceRoot = (Join-Path $env:RUNNER_TEMP 'gap0720-evidence')
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $EvidenceRoot -Force | Out-Null

function Write-Json {
  param([object]$InputObject, [string]$Path, [int]$Depth = 10)
  ConvertTo-Json -InputObject $InputObject -Depth $Depth | Set-Content -Encoding utf8 $Path
}

function Get-CorrelatedEvents {
  param(
    [string]$LogName,
    [Nullable[int]]$Id,
    [string]$PrimaryNeedle,
    [string]$FallbackNeedle,
    [DateTime]$StartTime,
    [DateTime]$EndTime
  )
  $filter = @{ LogName = $LogName; StartTime = $StartTime; EndTime = $EndTime }
  if ($null -ne $Id) { $filter.Id = [int]$Id }
  $events = @(Get-WinEvent -FilterHashtable $filter -ErrorAction SilentlyContinue)
  return @($events | Where-Object {
    $xml = $_.ToXml()
    ($PrimaryNeedle -and $xml.Contains($PrimaryNeedle)) -or
    ($FallbackNeedle -and $xml -match $FallbackNeedle)
  })
}

function Write-EventEvidence {
  param([object[]]$Events, [string]$Path)
  $out = @($Events | ForEach-Object {
    [ordered]@{
      log_name = $_.LogName
      provider = $_.ProviderName
      event_id = $_.Id
      record_id = $_.RecordId
      time_created = if ($_.TimeCreated) { $_.TimeCreated.ToUniversalTime().ToString('o') } else { $null }
      machine_name = $_.MachineName
      xml = $_.ToXml()
    }
  })
  Write-Json -InputObject $out -Path $Path -Depth 12
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$os = Get-CimInstance Win32_OperatingSystem
$tz = Get-TimeZone
$w32tm = (& w32tm /query /status 2>&1 | Out-String).Trim()

$environment = [ordered]@{
  schema_version = 'CEP_GAP0720_GITHUB_HOSTED_ENVIRONMENT_V1'
  evidence_scope = 'GITHUB_HOSTED_WINDOWS_LOCAL_TELEMETRY_ONLY'
  gap_id = 'D4-NEED-M4-GAP-0720'
  repository = $env:GITHUB_REPOSITORY
  ref = $env:GITHUB_REF
  sha = $env:GITHUB_SHA
  run_id = $env:GITHUB_RUN_ID
  run_attempt = $env:GITHUB_RUN_ATTEMPT
  runner_name = $env:RUNNER_NAME
  runner_os = $env:RUNNER_OS
  runner_arch = $env:RUNNER_ARCH
  image_os = $env:ImageOS
  image_version = $env:ImageVersion
  computer_name = $env:COMPUTERNAME
  user = $identity.Name
  elevated_administrator = $isAdmin
  os_caption = $os.Caption
  os_version = $os.Version
  os_build_number = $os.BuildNumber
  timezone_id = $tz.Id
  timezone_display_name = $tz.DisplayName
  utc_now = [DateTime]::UtcNow.ToString('o')
  w32tm_status = $w32tm
  edr_dimension = 'NOT_EXECUTED_NO_AUTHORIZED_EXTERNAL_EDR_BACKEND'
  full_gap_closure_claimed = $false
}
Write-Json $environment (Join-Path $EvidenceRoot '01_environment.json') 8
if (-not $isAdmin) { throw 'Runner is not elevated; telemetry configuration cannot proceed.' }

& auditpol.exe /set /subcategory:"Process Creation" /success:enable
if ($LASTEXITCODE -ne 0) { throw "auditpol failed: $LASTEXITCODE" }

$auditReg = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Policies\System\Audit'
New-Item -Path $auditReg -Force | Out-Null
New-ItemProperty -Path $auditReg -Name 'ProcessCreationIncludeCmdLine_Enabled' -PropertyType DWord -Value 1 -Force | Out-Null

$psReg = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\PowerShell\ScriptBlockLogging'
New-Item -Path $psReg -Force | Out-Null
New-ItemProperty -Path $psReg -Name 'EnableScriptBlockLogging' -PropertyType DWord -Value 1 -Force | Out-Null

& wevtutil.exe sl Microsoft-Windows-TaskScheduler/Operational /e:true
if ($LASTEXITCODE -ne 0) { throw "TaskScheduler channel enable failed: $LASTEXITCODE" }

(& auditpol.exe /get /subcategory:"Process Creation" 2>&1 | Out-String) |
  Set-Content -Encoding utf8 (Join-Path $EvidenceRoot 'audit-policy.txt')
Write-Json (Get-ItemProperty $auditReg | Select-Object ProcessCreationIncludeCmdLine_Enabled) (Join-Path $EvidenceRoot 'security-audit-registry.json') 4
Write-Json (Get-ItemProperty $psReg | Select-Object EnableScriptBlockLogging) (Join-Path $EvidenceRoot 'powershell-logging-registry.json') 4

$sysmonDir = Join-Path $env:RUNNER_TEMP 'cep-gap0720-sysmon'
New-Item -ItemType Directory -Path $sysmonDir -Force | Out-Null
$zip = Join-Path $sysmonDir 'Sysmon.zip'
Invoke-WebRequest -Uri 'https://download.sysinternals.com/files/Sysmon.zip' -OutFile $zip -UseBasicParsing
Write-Json (Get-FileHash -Algorithm SHA256 $zip | Select-Object Path, Algorithm, Hash) (Join-Path $EvidenceRoot 'sysmon-download-sha256.json') 4
Expand-Archive -Path $zip -DestinationPath $sysmonDir -Force
$exe = Join-Path $sysmonDir 'Sysmon64.exe'
if (-not (Test-Path $exe)) { throw 'Sysmon64.exe missing after extraction.' }

$config = @(
  '<Sysmon schemaversion="4.50">',
  '  <HashAlgorithms>SHA256</HashAlgorithms>',
  '  <EventFiltering>',
  '    <ProcessCreate onmatch="exclude" />',
  '  </EventFiltering>',
  '</Sysmon>'
) -join [Environment]::NewLine
$configPath = Join-Path $sysmonDir 'gap0720-sysmon.xml'
$config | Set-Content -Encoding ascii $configPath
Copy-Item $configPath (Join-Path $EvidenceRoot 'sysmon-config.xml') -Force
Write-Json (Get-FileHash -Algorithm SHA256 $configPath | Select-Object Path, Algorithm, Hash) (Join-Path $EvidenceRoot 'sysmon-config-sha256.json') 4

$existing = Get-Service -Name Sysmon64,Sysmon -ErrorAction SilentlyContinue
if ($existing) { & $exe -c $configPath } else { & $exe -accepteula -i $configPath }
if ($LASTEXITCODE -ne 0) { throw "Sysmon configuration failed: $LASTEXITCODE" }
(& $exe -c 2>&1 | Out-String) | Set-Content -Encoding utf8 (Join-Path $EvidenceRoot 'sysmon-effective-config.txt')
Write-Json (Get-WinEvent -ListLog 'Microsoft-Windows-Sysmon/Operational' | Select-Object LogName, IsEnabled, RecordCount, LogMode, MaximumSizeInBytes) (Join-Path $EvidenceRoot 'sysmon-channel-state.json') 4

$runId = [guid]::NewGuid().Guid
$taskName = "CEP-GAP0720-$runId"
$actionDir = Join-Path $env:RUNNER_TEMP "gap0720-action-$runId"
New-Item -ItemType Directory -Path $actionDir -Force | Out-Null
$scriptPath = Join-Path $actionDir "known-action-$runId.ps1"
$markerPath = Join-Path $actionDir "marker-$runId.txt"
$escapedMarker = $markerPath.Replace("'","''")
$knownAction = @(
  '$ErrorActionPreference = ''Stop''',
  ('$RunId = ''{0}''' -f $runId),
  ('$MarkerPath = ''{0}''' -f $escapedMarker),
  '$Utc = [DateTime]::UtcNow.ToString(''o'')',
  '"CEP-GAP0720|$RunId|$Utc" | Set-Content -Encoding utf8 -Path $MarkerPath',
  'Start-Process -FilePath "$env:SystemRoot\System32\cmd.exe" -ArgumentList "/c echo $RunId>nul" -Wait'
) -join [Environment]::NewLine
$knownAction | Set-Content -Encoding utf8 $scriptPath

$taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $scriptPath)
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Description "CEP GAP-0720 benign known action $runId" -Force | Out-Null

$t0Utc = [DateTime]::UtcNow
$stopwatch = [Diagnostics.Stopwatch]::StartNew()
Start-ScheduledTask -TaskName $taskName
$deadline = [DateTime]::UtcNow.AddSeconds(45)
while (-not (Test-Path $markerPath)) {
  if ([DateTime]::UtcNow -ge $deadline) { throw "Known-action marker timeout: $markerPath" }
  Start-Sleep -Milliseconds 500
}
Start-Sleep -Seconds 10
$stopwatch.Stop()
$t1Utc = [DateTime]::UtcNow

$groundTruth = [ordered]@{
  schema_version = 'CEP_GAP0720_KNOWN_ACTION_V1'
  gap_id = 'D4-NEED-M4-GAP-0720'
  action_class = 'BENIGN_SCHEDULED_POWERSHELL_MARKER'
  run_id = $runId
  task_name = $taskName
  script_path = $scriptPath
  marker_path = $markerPath
  t0_utc = $t0Utc.ToString('o')
  t1_utc = $t1Utc.ToString('o')
  monotonic_elapsed_ms = [math]::Round($stopwatch.Elapsed.TotalMilliseconds, 3)
  marker_content = (Get-Content -Raw $markerPath).Trim()
}
Write-Json $groundTruth (Join-Path $EvidenceRoot '02_ground_truth.json') 8

$start = $t0Utc.AddSeconds(-5)
$end = [DateTime]::UtcNow.AddSeconds(5)
$security = @(Get-CorrelatedEvents 'Security' 4688 $runId '(?i)powershell\.exe' $start $end)
$sysmon = @(Get-CorrelatedEvents 'Microsoft-Windows-Sysmon/Operational' 1 $runId '(?i)powershell\.exe' $start $end)
$powershell = @(Get-CorrelatedEvents 'Microsoft-Windows-PowerShell/Operational' 4104 $runId $null $start $end)
$task = @(Get-CorrelatedEvents 'Microsoft-Windows-TaskScheduler/Operational' $null $taskName $null $start $end)

Write-EventEvidence $security (Join-Path $EvidenceRoot '03_security_4688.json')
Write-EventEvidence $sysmon (Join-Path $EvidenceRoot '04_sysmon_event1.json')
Write-EventEvidence $powershell (Join-Path $EvidenceRoot '05_powershell_4104.json')
Write-EventEvidence $task (Join-Path $EvidenceRoot '06_taskscheduler.json')

& wevtutil.exe epl Security (Join-Path $EvidenceRoot 'Security.evtx')
& wevtutil.exe epl Microsoft-Windows-Sysmon/Operational (Join-Path $EvidenceRoot 'Sysmon.evtx')
& wevtutil.exe epl Microsoft-Windows-PowerShell/Operational (Join-Path $EvidenceRoot 'PowerShell.evtx')
& wevtutil.exe epl Microsoft-Windows-TaskScheduler/Operational (Join-Path $EvidenceRoot 'TaskScheduler.evtx')

$edr = [ordered]@{
  schema_version = 'CEP_GAP0720_EDR_DIMENSION_V1'
  status = 'NOT_EXECUTED'
  reason = 'No Controller-authorized external EDR tenant/backend is bound to this GitHub-hosted mission.'
  local_windows_defender_is_not_treated_as_external_edr_backend = $true
  fabricated_or_simulated_edr_evidence = $false
  full_gap_closure_claimed = $false
}
Write-Json $edr (Join-Path $EvidenceRoot '07_edr_dimension.json') 5

$matrix = @(
  [pscustomobject]@{provider='Windows Security'; target='4688'; found=($security.Count -gt 0); count=$security.Count; correlation='RunId when command line available; otherwise narrow known-action PowerShell window'}
  [pscustomobject]@{provider='Sysmon'; target='Event ID 1'; found=($sysmon.Count -gt 0); count=$sysmon.Count; correlation='RunId/PowerShell command line; ProcessGuid retained in raw XML'}
  [pscustomobject]@{provider='PowerShell'; target='4104'; found=($powershell.Count -gt 0); count=$powershell.Count; correlation='RunId embedded in executed script block'}
  [pscustomobject]@{provider='Task Scheduler'; target='Operational'; found=($task.Count -gt 0); count=$task.Count; correlation='Exact TaskName carrying RunId'}
  [pscustomobject]@{provider='EDR'; target='External backend telemetry'; found=$false; count=0; correlation='NOT_EXECUTED on GitHub-hosted runner'}
)
$matrix | Export-Csv -NoTypeInformation -Encoding utf8 (Join-Path $EvidenceRoot '08_correlation_matrix.csv')
Write-Json $matrix (Join-Path $EvidenceRoot '08_correlation_matrix.json') 6

$missing = @($matrix | Where-Object { -not $_.found } | ForEach-Object {
  [ordered]@{
    provider = $_.provider
    target = $_.target
    classification = if ($_.provider -eq 'EDR') { 'EXPECTED_NON_EXECUTED_DIMENSION' } else { 'UNEXPECTED_LOCAL_TELEMETRY_MISSING' }
  }
})
$missingAnalysis = [ordered]@{
  schema_version = 'CEP_GAP0720_MISSING_EVENT_ANALYSIS_V1'
  run_id = $runId
  missing = $missing
  edr_is_known_blocker = $true
}
Write-Json $missingAnalysis (Join-Path $EvidenceRoot '09_missing_event_analysis.json') 8

$localFound = @($matrix | Where-Object { $_.provider -ne 'EDR' -and $_.found }).Count
$validation = [ordered]@{
  schema_version = 'CEP_GAP0720_GITHUB_HOSTED_VALIDATION_V1'
  gap_id = 'D4-NEED-M4-GAP-0720'
  run_id = $runId
  local_provider_target_count = 4
  local_provider_found_count = $localFound
  local_provider_gate_pass = ($localFound -eq 4)
  edr_gate_pass = $false
  full_gap_closure = $false
  classification = if ($localFound -eq 4) { 'GITHUB_HOSTED_WINDOWS_4_PROVIDER_EVIDENCE_COMPLETE__EDR_NOT_EXECUTED' } else { 'GITHUB_HOSTED_WINDOWS_PARTIAL_PROVIDER_EVIDENCE__LOCAL_GAPS_AND_EDR_NOT_EXECUTED' }
  controller_adjudication_required = $true
}
Write-Json $validation (Join-Path $EvidenceRoot '10_validation.json') 8

$channels = Get-WinEvent -ListLog Security,'Microsoft-Windows-Sysmon/Operational','Microsoft-Windows-PowerShell/Operational','Microsoft-Windows-TaskScheduler/Operational' |
  Select-Object LogName, IsEnabled, RecordCount, LogMode, MaximumSizeInBytes
Write-Json $channels (Join-Path $EvidenceRoot 'channel-state-after.json') 4

Get-ChildItem -Path $EvidenceRoot -File |
  Sort-Object Name |
  Select-Object Name, Length, LastWriteTimeUtc |
  ConvertTo-Json -Depth 4 |
  Set-Content -Encoding utf8 (Join-Path $EvidenceRoot 'evidence-manifest.json')

Get-ChildItem -Path $EvidenceRoot -File |
  Where-Object { $_.Name -ne 'SHA256SUMS.txt' } |
  Sort-Object Name |
  ForEach-Object {
    $h = Get-FileHash -Algorithm SHA256 $_.FullName
    "$($h.Hash.ToLowerInvariant())  $($_.Name)"
  } |
  Set-Content -Encoding ascii (Join-Path $EvidenceRoot 'SHA256SUMS.txt')

Write-Host "GAP0720_RUN_ID=$runId"
Write-Host "LOCAL_PROVIDER_COUNT=$localFound/4"
Write-Host "CLASSIFICATION=$($validation.classification)"
if (-not $validation.local_provider_gate_pass) {
  throw 'One or more local telemetry providers did not produce correlated evidence.'
}
