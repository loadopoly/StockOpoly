#Requires -Version 5.1
<#
.SYNOPSIS
    Registers the x_osq Holon with Holon Hub (HubCore) and rebuilds the HubCore agent package.

.DESCRIPTION
    1. Validates holon.json, the root SKILL.md, every stage skill, and every stage script.
    2. Assembles skills/x-osq into hubcore/extracted (generated folder; osq/ is the source of truth).
    3. Upserts the skill, conversation starters, and Holon registry block into declarativeAgent_0.json.
    4. Upserts hubcore/holons/registry.json.
    5. Bumps the Teams/Copilot app manifest patch version only when Holon content changed.
    6. Backs up HubCore.zip, rebuilds it with forward-slash entry names, and verifies the package.
    Idempotent: re-running with unchanged content changes nothing except verification output.

.PARAMETER Check
    Validate and report what would change without writing anything.
#>
[CmdletBinding()]
param([switch]$Check)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem

$OsqRoot      = Split-Path -Parent $PSScriptRoot
$HubCoreRoot  = Join-Path (Split-Path -Parent $OsqRoot) 'hubcore'
$Extracted    = Join-Path $HubCoreRoot 'extracted'
$ZipPath      = Join-Path $HubCoreRoot 'HubCore.zip'
$RegistryPath = Join-Path $HubCoreRoot 'holons\registry.json'
$DeepWorkRoot = Split-Path -Parent (Split-Path -Parent $OsqRoot)
$AuditLog     = Join-Path $DeepWorkRoot 'logs\agentic_audit.jsonl'
$Utf8NoBom    = New-Object System.Text.UTF8Encoding($false)
$InstructionLimit = 8000

function Read-Json([string]$Path) { [IO.File]::ReadAllText($Path, $Utf8NoBom) | ConvertFrom-Json }
function Write-Json([string]$Path, $Object) {
    $json = $Object | ConvertTo-Json -Depth 30
    if (-not $Check) { [IO.File]::WriteAllText($Path, $json + "`n", $Utf8NoBom) }
}
function Get-Sha256([string]$Path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() }
function Get-RelPath([string]$Base, [string]$Full) { $Full.Substring($Base.TrimEnd('\').Length + 1).Replace('\', '/') }
function Assert([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "[FAIL] $Message" } }

# ---------------------------------------------------------------- 1. validate
$holon = Read-Json (Join-Path $OsqRoot 'holon.json')
$hid   = $holon.holon.id
$skillFolderRel = $holon.hub.agentSkillFolder                     # skills/x-osq
$skillLeaf = Split-Path -Leaf $skillFolderRel
Assert ($hid -and $holon.holon.version -match '^\d+\.\d+\.\d+$') 'holon.json needs holon.id and semver holon.version'

$rootSkill = [IO.File]::ReadAllText((Join-Path $OsqRoot 'SKILL.md'), $Utf8NoBom)
Assert ($rootSkill -match "(?m)^name:\s*$([regex]::Escape($skillLeaf))\s*$") "SKILL.md frontmatter name must be '$skillLeaf'"

$declared = @($holon.stages | ForEach-Object { $_.skill })
$onDisk = @(Get-ChildItem -LiteralPath $OsqRoot -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'SKILL.md') } | ForEach-Object { $_.Name })
$undeclared = @($onDisk | Where-Object { $declared -notcontains $_ })
Assert ($undeclared.Count -eq 0) "skill folder(s) not declared in holon.json stages: $($undeclared -join ', ')"
$skillBody = [IO.File]::ReadAllText((Join-Path $OsqRoot 'SKILL.md'), $Utf8NoBom)
$unrouted = @($declared | Where-Object { $skillBody -notmatch [regex]::Escape("($_/SKILL.md)") })
Assert ($unrouted.Count -eq 0) "SKILL.md does not link stage(s): $($unrouted -join ', ')"

$PackageExtensions = @('.md', '.json', '.yaml', '.yml', '.py', '.txt', '.html', '.ps1')
$include = New-Object System.Collections.Generic.List[string]
$include.Add('SKILL.md'); $include.Add('holon.json'); $include.Add('holon/x_osq.py')
if (Test-Path (Join-Path $OsqRoot 'Module.html')) { $include.Add('Module.html') }
if (Test-Path (Join-Path $OsqRoot 'x_OSQ.html')) { $include.Add('x_OSQ.html') }
if (Test-Path (Join-Path $OsqRoot 'OSQ.html')) { $include.Add('OSQ.html') }
if (Test-Path (Join-Path $OsqRoot 'x_CRM.html')) { $include.Add('x_CRM.html') }
if (Test-Path (Join-Path $OsqRoot 'holon\Start-HolonHubLive.ps1')) { $include.Add('holon/Start-HolonHubLive.ps1') }
if (Test-Path (Join-Path $OsqRoot 'holon\Invoke-OsqPipeline.ps1')) { $include.Add('holon/Invoke-OsqPipeline.ps1') }
foreach ($stage in $holon.stages) {
    $dir = Join-Path $OsqRoot $stage.skill
    Assert (Test-Path (Join-Path $dir 'SKILL.md')) "stage $($stage.skill) is missing SKILL.md"
    Get-ChildItem -LiteralPath $dir -Recurse -File |
        Where-Object { $_.FullName -notmatch '\\(__pycache__|out)\\' -and $PackageExtensions -contains $_.Extension.ToLowerInvariant() } |
        ForEach-Object { $include.Add((Get-RelPath $OsqRoot $_.FullName)) }
}
if (Test-Path (Join-Path $OsqRoot 'holon\samples')) {
    Get-ChildItem -LiteralPath (Join-Path $OsqRoot 'holon\samples') -File | ForEach-Object { $include.Add((Get-RelPath $OsqRoot $_.FullName)) }
}

# every script referenced by the runner must ship
$runner = [IO.File]::ReadAllText((Join-Path $OsqRoot 'holon\x_osq.py'), $Utf8NoBom)
foreach ($m in [regex]::Matches($runner, '"([a-z0-9-]+/scripts/[a-z_]+\.py)"')) {
    Assert ($include -contains $m.Groups[1].Value) "runner references missing script $($m.Groups[1].Value)"
}
foreach ($rel in $include) { if ($rel -like '*.json') { $null = Read-Json (Join-Path $OsqRoot $rel) } }
Write-Host "[PASS] validated $hid v$($holon.holon.version): $($holon.stages.Count) stages, $($include.Count) files" -ForegroundColor Green

# content hash = sha256 over sorted "path:sha256" lines
$sorted = $include | Sort-Object -Unique
$lines  = $sorted | ForEach-Object { "{0}:{1}" -f $_, (Get-Sha256 (Join-Path $OsqRoot $_)) }
$sha = [Security.Cryptography.SHA256]::Create()
$contentSha = -join ($sha.ComputeHash($Utf8NoBom.GetBytes(($lines -join "`n"))) | ForEach-Object { $_.ToString('x2') })

$registry = if (Test-Path $RegistryPath) { Read-Json $RegistryPath } else { $null }
$existing = @(if ($registry) { $registry.holons | Where-Object { $_.holon_id -eq $hid } })
$changed  = -not ($existing.Count -eq 1 -and $existing[0].contentSha256 -eq $contentSha -and $existing[0].version -eq $holon.holon.version)
Write-Host ("[INFO] content sha256 {0} -> {1}" -f $contentSha.Substring(0,12), $(if ($changed) { 'CHANGED' } else { 'unchanged' }))

# ---------------------------------------------------------------- 2. assemble skill folder
$target = Join-Path $Extracted ($skillFolderRel.Replace('/', '\'))
Assert ($target.StartsWith((Join-Path $Extracted 'skills\'), [StringComparison]::OrdinalIgnoreCase)) 'refusing to write outside hubcore/extracted/skills'
$copied = 0; $removed = 0
if (-not $Check) {
    $wanted = @{}; foreach ($rel in $sorted) { $wanted[$rel] = $true }
    foreach ($rel in $sorted) {
        $src = Join-Path $OsqRoot ($rel.Replace('/', '\'))
        $dst = Join-Path $target ($rel.Replace('/', '\'))
        $null = New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst)
        if (-not (Test-Path -LiteralPath $dst) -or (Get-Sha256 $dst) -ne (Get-Sha256 $src)) {
            Copy-Item -LiteralPath $src -Destination $dst -Force; $copied++
        }
    }
    if (Test-Path -LiteralPath $target) {
        Get-ChildItem -LiteralPath $target -Recurse -File |
            Where-Object { -not $wanted.ContainsKey((Get-RelPath $target $_.FullName)) } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force; $removed++ }
    }
}
Write-Host "[PASS] synced $skillFolderRel ($($sorted.Count) files; $copied copied, $removed stale removed)" -ForegroundColor Green

# ---------------------------------------------------------------- 3. declarative agent
$daPath = Join-Path $Extracted 'declarativeAgent_0.json'
$da = Read-Json $daPath
$skills = @($da.agent_skills | Where-Object { $_.folder -ne $skillFolderRel }) + @([pscustomobject]@{ folder = $skillFolderRel })
$da.agent_skills = $skills

$starters = @(
    [pscustomobject]@{ title = 'OSQ: ingest CPQ quotes';             text = 'Use x_OSQ to ingest raw CPQ / Salesforce / SAP quote feeds, validate the 26-week horizon, and filter accepted-quote eligibility.' },
    [pscustomobject]@{ title = 'OSQ: cleanse forecast & deltas';     text = 'Run the x_OSQ forecast cleanser to execute calendar proration, detect deltas vs previous publication, and eliminate duplicate demand (retiring Snowflake logic).' },
    [pscustomobject]@{ title = 'OSQ: QUIPU forecast intelligence';   text = 'Engage QUIPU inside Holon Hub via x_OSQ to score forecast confidence, suppress 70%+ over-forecasting anomalies, and manage SKU substitutions without duplicate demand.' },
    [pscustomobject]@{ title = 'OSQ: simulate lead times & front-load'; text = 'Simulate import vs domestic lead-time sensitivity and evaluate front-loading risk for RenoPlus project demand using x_OSQ and QUIPU.' },
    [pscustomobject]@{ title = 'OSQ: evaluate Emergency Brake';      text = 'Check the x_OSQ Emergency Brake exception status to determine if low confidence or abnormal import volume triggers a governance hold.' },
    [pscustomobject]@{ title = 'OSQ: transform for Blue Yonder & SAP'; text = 'Transform cleansed, governed demand into Blue Yonder 26-week replenishment buckets and synchronize SAP material reservations.' }
)
$titles = $starters | ForEach-Object { $_.title }
$da.conversation_starters = @(@($da.conversation_starters | Where-Object { $titles -notcontains $_.title }) + $starters)

$begin = '## Registered Holons'
$osqBlock = "- x_osq v$($holon.holon.version) (skill: $skillLeaf) - HolonHubLive (:8900) capabilities osq.forecast-intake, osq.forecast-cleansing, osq.quipu-intelligence, osq.forecast-governance, osq.by-transformation, osq.integration-connectors across $($holon.stages.Count) stages: ingest CPQ/SFDC/SAP, direct API time-phased QOH (Snowflake analytics-only), QUIPU note classification & 70%+ suppression, Dijkstra physical inventory marketplace, IFR auto-PO & cancel-quarantine, RFID telemetry, Stockopoly & SAP signal bridges."

$instr = [string]$da.instructions
$idx = $instr.IndexOf("`n$begin")
$existingHolons = @()
if ($idx -ge 0) {
    $existingBlock = $instr.Substring($idx)
    $lines = $existingBlock -split "`n"
    foreach ($l in $lines) {
        $trimmed = $l.Trim()
        if ($trimmed.StartsWith('- x_') -and -not $trimmed.StartsWith('- x_osq ')) {
            $existingHolons += $trimmed
        }
    }
    $instr = $instr.Substring(0, $idx)
}

$allHolons = @($existingHolons) + @($osqBlock)
$finalBlock = "$begin`n" + ($allHolons -join "`n")
$da.instructions = $instr.TrimEnd() + "`n`n" + $finalBlock.Trim()
Assert ($da.instructions.Length -le $InstructionLimit) "instructions length $($da.instructions.Length) exceeds $InstructionLimit"
Write-Json $daPath $da
Write-Host "[PASS] declarativeAgent_0.json: agent_skills=$(@($da.agent_skills).Count), starters=$(@($da.conversation_starters).Count), instructions=$($da.instructions.Length)/$InstructionLimit chars" -ForegroundColor Green

# ---------------------------------------------------------------- 4. app manifest version
$mfPath = Join-Path $Extracted 'manifest.json'
$mf = Read-Json $mfPath
$oldVersion = $mf.version
if ($changed) {
    $v = $oldVersion.Split('.'); $v[2] = [string]([int]$v[2] + 1); $mf.version = $v -join '.'
    Write-Json $mfPath $mf
}
Write-Host "[PASS] manifest.json version $oldVersion -> $($mf.version)" -ForegroundColor Green

# ---------------------------------------------------------------- 5. holon registry
$now = (Get-Date).ToUniversalTime().ToString('o')
$entry = [pscustomobject]@{
    holon_id         = $hid
    name             = $holon.holon.name
    version          = $holon.holon.version
    capabilities     = @($holon.capabilities | ForEach-Object { $_.id })
    operations       = @($holon.capabilities | ForEach-Object { $_.operations } | ForEach-Object { $_.id })
    agentSkillFolder = $skillFolderRel
    manifestPath     = 'm365-copilot/osq/holon.json'
    entrypoint       = $holon.holon.entrypoint
    events           = $holon.events
    dependencies     = @($holon.dependencies)
    contentSha256    = $contentSha
    packageVersion   = $mf.version
    registeredAtUtc  = $(if ($changed -or $existing.Count -eq 0) { $now } else { $existing[0].registeredAtUtc })
}
$others = @(if ($registry) { $registry.holons | Where-Object { $_.holon_id -ne $hid } })
$newRegistry = [pscustomobject]@{
    hub          = 'HubCore'
    kernel       = 'x_hubcore'
    appId        = $mf.id
    description  = 'Holons registered with Holon Hub. Maintained by Register-HolonHub.ps1 registration scripts; one entry per holon_id.'
    updatedAtUtc = $(if ($changed -or -not $registry) { $now } else { $registry.updatedAtUtc })
    holons       = @($others + $entry)
}
if (-not $Check) { $null = New-Item -ItemType Directory -Force -Path (Split-Path -Parent $RegistryPath) }
Write-Json $RegistryPath $newRegistry
Write-Host "[PASS] holon registry: $(@($newRegistry.holons).Count) holon(s)" -ForegroundColor Green

# ---------------------------------------------------------------- 6. package
if (-not $Check) {
    $backup = Join-Path $HubCoreRoot "HubCore.v$oldVersion.zip"
    if ($changed -and (Test-Path $ZipPath) -and -not (Test-Path $backup)) { Copy-Item -LiteralPath $ZipPath -Destination $backup }
    $tmp = "$ZipPath.tmp"
    if (Test-Path $tmp) { Remove-Item -LiteralPath $tmp }
    $zip = [IO.Compression.ZipFile]::Open($tmp, [IO.Compression.ZipArchiveMode]::Create)
    try {
        Get-ChildItem -LiteralPath $Extracted -Recurse -File | Sort-Object FullName | ForEach-Object {
            $null = [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, (Get-RelPath $Extracted $_.FullName), [IO.Compression.CompressionLevel]::Optimal)
        }
    } finally { $zip.Dispose() }
    Move-Item -LiteralPath $tmp -Destination $ZipPath -Force
}

# ---------------------------------------------------------------- 7. verify package
$verifyZip = if ($Check) { $null } else { $ZipPath }
if ($verifyZip) {
    $zip = [IO.Compression.ZipFile]::OpenRead($verifyZip)
    try {
        $names = @($zip.Entries | ForEach-Object { $_.FullName })
        Assert (-not ($names | Where-Object { $_ -match '\\' })) 'zip entries must use forward slashes'
        foreach ($req in @('manifest.json', 'declarativeAgent_0.json', 'color.png', 'outline.png')) { Assert ($names -contains $req) "zip missing $req" }
        foreach ($e in $zip.Entries | Where-Object { $_.FullName -like '*.json' }) {
            $r = New-Object IO.StreamReader($e.Open(), $Utf8NoBom)
            try { $null = $r.ReadToEnd() | ConvertFrom-Json } finally { $r.Dispose() }
        }
        $r = New-Object IO.StreamReader(($zip.Entries | Where-Object { $_.FullName -eq 'declarativeAgent_0.json' }).Open(), $Utf8NoBom)
        try { $zda = $r.ReadToEnd() | ConvertFrom-Json } finally { $r.Dispose() }
        foreach ($s in $zda.agent_skills) { Assert ($names -contains "$($s.folder)/SKILL.md") "agent skill $($s.folder) has no SKILL.md in package" }
        foreach ($rel in $sorted) { Assert ($names -contains "$skillFolderRel/$rel") "package missing $skillFolderRel/$rel" }
        $secretHits = @($zip.Entries | Where-Object { $_.FullName -match '\.(json|md|py)$' } | Where-Object {
            $r = New-Object IO.StreamReader($_.Open(), $Utf8NoBom); try { $t = $r.ReadToEnd() } finally { $r.Dispose() }
            $t -match 'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.' -or $t -match '-----BEGIN (RSA |EC )?PRIVATE KEY-----'
        })
        Assert ($secretHits.Count -eq 0) "token/key material found in: $(($secretHits | ForEach-Object FullName) -join ', ')"
        Write-Host "[PASS] HubCore.zip v$($mf.version): $($names.Count) entries, $(@($zda.agent_skills).Count) agent skills, JSON valid, no credential material" -ForegroundColor Green
    } finally { $zip.Dispose() }
}

if ($changed -and -not $Check -and (Test-Path (Split-Path -Parent $AuditLog))) {
    $log = [ordered]@{ timestamp = $now; source = 'register-holon-hub'; action = 'holon.registered'
        details = [ordered]@{ holon = $hid; version = $holon.holon.version; packageVersion = $mf.version; contentSha256 = $contentSha } }
    [IO.File]::AppendAllText($AuditLog, (($log | ConvertTo-Json -Compress -Depth 5) + "`n"), $Utf8NoBom)
}

[pscustomobject]@{ holon = $hid; version = $holon.holon.version; packageVersion = $mf.version; changed = $changed; checkOnly = [bool]$Check; files = $sorted.Count } | ConvertTo-Json
