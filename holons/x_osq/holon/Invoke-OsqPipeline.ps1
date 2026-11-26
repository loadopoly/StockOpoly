#Requires -Version 5.1
<#
.SYNOPSIS
    Native PowerShell 5.1 pipeline runner for x_OSQ Holon (zero external dependencies).
.DESCRIPTION
    Executes all 6 stages of x_OSQ:
      1. Intake Normalization & 26-week horizon / accepted-quote gating
      2. Forecast Cleansing (Snowflake logic retirement: proration, deltas, deduplication)
      3. QUIPU participant intelligence (confidence scoring, 70%+ over-forecast suppression, SKU substitution, lead times)
      4. Governance Emergency Brake evaluation and held-task workflows
      5. Blue Yonder 26-week weekly bucket transformation & front-loading realization
      6. Integration Connectors (Blue Yonder export & SAP material reservations)
#>
[CmdletBinding()]
param(
    [string]$InputPath,
    [string]$PreviousPath,
    [string]$Mode = "adaptive",
    [string]$Plant = "PLANT-1010",
    [string]$OutDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $InputPath) { $InputPath = Join-Path $ScriptDir 'samples\sample_cpq_quotes.json' }
if (-not $PreviousPath) { $PreviousPath = Join-Path $ScriptDir 'samples\sample_previous_publication.json' }
if (-not $OutDir) { $OutDir = Join-Path (Split-Path -Parent $ScriptDir) 'out' }

function Read-Json([string]$Path) { [IO.File]::ReadAllText($Path, $Utf8NoBom) | ConvertFrom-Json }
function Write-Json([string]$Path, $Object) {
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { $null = New-Item -ItemType Directory -Force -Path $dir }
    $json = $Object | ConvertTo-Json -Depth 25
    [IO.File]::WriteAllText($Path, $json + "`n", $Utf8NoBom)
}
function Get-Prop($Obj, [string]$Name, $Default = $null) {
    if ($null -eq $Obj) { return $Default }
    $p = $Obj.PSObject.Properties[$Name]
    if ($null -ne $p -and $null -ne $p.Value) { return $p.Value }
    return $Default
}
function New-Bag { ,(New-Object System.Collections.ArrayList) }
function Add-Bag($Bag, $Item) { [void]$Bag.Add($Item) }

Write-Host "=== Executing x_OSQ Native Pipeline Runner [Mode: $($Mode.ToUpper())] ===" -ForegroundColor Cyan

$resolvedOut = [System.IO.Path]::GetFullPath($OutDir)
if (-not (Test-Path $resolvedOut)) { $null = New-Item -ItemType Directory -Force -Path $resolvedOut }

# ---------------- Stage 1: Intake Normalizer ----------------
Write-Host "[1/6] Running Stage 1: OSQ Intake Normalizer..." -ForegroundColor Green
$raw = Read-Json $InputPath
$acceptedLines = New-Bag
$ineligibleLines = New-Bag
$rawTotalUnits = 0.0

foreach ($q in $raw.quotes) {
    $status = Get-Prop $q 'status' ''
    $elig = Get-Prop $q 'eligibility' ''
    $isAccepted = ($status -eq 'ACCEPTED' -or $elig -eq 'FORECAST_ELIGIBLE')
    if (-not $isAccepted) {
        foreach ($l in $q.lines) {
            Add-Bag $ineligibleLines ([pscustomobject]@{
                quoteId = $q.quoteId
                lineId  = $l.lineId
                sku     = $l.sku
                reason  = "Quote status '$status' is ineligible for forecast publication (accepted quotes only)."
            })
        }
        continue
    }

    foreach ($l in $q.lines) {
        $validWeekly = New-Bag
        foreach ($wf in $l.weeklyForecast) {
            $wNum = [int]$wf.week
            $qty = [double]$wf.quantity
            $rawTotalUnits += $qty
            $cmProrated = [bool](Get-Prop $wf 'currentMonthProrated' $false)
            if ($wNum -ge 1 -and $wNum -le 26) {
                Add-Bag $validWeekly ([pscustomobject]@{
                    week = $wNum
                    quantity = $qty
                    currentMonthProrated = $cmProrated
                })
            }
        }

        Add-Bag $acceptedLines ([pscustomobject]@{
            quoteId                    = $q.quoteId
            lineId                     = $l.lineId
            customer                   = Get-Prop $q 'customer' 'UNKNOWN_CUSTOMER'
            sku                        = $l.sku
            description                = Get-Prop $l 'description' ''
            origin                     = [string](Get-Prop $l 'origin' 'DOMESTIC')
            leadTimeWeeks              = [int](Get-Prop $l 'leadTimeWeeks' 2)
            weeklyForecast             = @($validWeekly)
            frontLoadRequested         = [bool](Get-Prop $l 'frontLoadRequested' $false)
            frontLoadWeeks             = [int](Get-Prop $l 'frontLoadWeeks' 0)
            historicalActualMonthlyAvg = [double](Get-Prop $l 'historicalActualMonthlyAvg' 50.0)
            salesRepConfidence         = [double](Get-Prop $l 'salesRepConfidence' 0.8)
            replacementSku             = Get-Prop $l 'replacementSku' $null
            replacementEffectiveWeek   = Get-Prop $l 'replacementEffectiveWeek' $null
            source                     = Get-Prop $q 'source' 'CPQ'
        })
    }
}

$intakeEnvelope = [pscustomobject]@{
    envelope = [pscustomobject]@{
        type = "OSQ_INTAKE_STREAM"
        version = "1.0.0"
        timestampUtc = (Get-Date).ToUniversalTime().ToString("o")
        batchId = $raw.batchId
        horizonWeeks = 26
    }
    summary = [pscustomobject]@{
        total_quotes_received  = @($raw.quotes).Count
        accepted_quotes        = @($raw.quotes | Where-Object { $_.status -eq 'ACCEPTED' }).Count
        ineligible_quotes      = @($raw.quotes | Where-Object { $_.status -ne 'ACCEPTED' }).Count
        total_lines_accepted   = $acceptedLines.Count
        total_lines_ineligible = $ineligibleLines.Count
        total_units_raw        = $rawTotalUnits
    }
    acceptedDemandLines = @($acceptedLines)
    ineligibleExclusions = @($ineligibleLines)
}
$fIntake = Join-Path $resolvedOut "01_intake_normalized.json"
Write-Json $fIntake $intakeEnvelope

# ---------------- Stage 2: Forecast Cleanser ----------------
Write-Host "[2/6] Running Stage 2: OSQ Forecast Cleanser (Retires Snowflake)..." -ForegroundColor Green
$prevPub = if (Test-Path $PreviousPath) { Read-Json $PreviousPath } else { $null }
$cleansedLines = New-Bag
$dedupExclusions = New-Bag
$deltas = New-Bag
$seenKeys = New-Object System.Collections.Generic.HashSet[string]
$prorationFactor = 0.75

foreach ($line in $acceptedLines) {
    $dedupKey = "$($line.customer)|$($line.sku)"
    if ($seenKeys.Contains($dedupKey)) {
        Add-Bag $dedupExclusions ([pscustomobject]@{
            quoteId = $line.quoteId; lineId = $line.lineId; sku = $line.sku
            reason = "Duplicate demand signature identified across concurrent quote entries."
        })
        continue
    }
    $null = $seenKeys.Add($dedupKey)

    $cleansedWeekly = New-Bag
    foreach ($wf in $line.weeklyForecast) {
        $wNum = [int]$wf.week
        $qty = [double]$wf.quantity
        $prorated = $false
        $isProrated = [bool](Get-Prop $wf 'currentMonthProrated' $false)
        if ($wNum -eq 1 -or $isProrated) {
            $qty = [Math]::Round($qty * $prorationFactor, 2)
            $prorated = $true
        }

        $prevQty = 0.0
        if ($prevPub) {
            $pubDemand = Get-Prop $prevPub 'publishedWeeklyDemand' $null
            if ($pubDemand) {
                $skuDemand = Get-Prop $pubDemand $line.sku $null
                if ($skuDemand) {
                    $val = Get-Prop $skuDemand "$wNum" $null
                    if ($null -ne $val) { $prevQty = [double]$val }
                }
            }
        }
        $delta = [Math]::Round($qty - $prevQty, 2)
        if ([Math]::Abs($delta) -gt 0.01) {
            Add-Bag $deltas ([pscustomobject]@{
                sku = $line.sku; week = $wNum; previousQty = $prevQty; newQty = $qty; delta = $delta
            })
        }

        Add-Bag $cleansedWeekly ([pscustomobject]@{
            week = $wNum
            rawQuantity = $wf.quantity
            cleansedQuantity = $qty
            prorated = $prorated
            deltaVsPrevious = $delta
        })
    }

    $cLine = [pscustomobject]@{
        quoteId                    = $line.quoteId
        lineId                     = $line.lineId
        customer                   = $line.customer
        sku                        = $line.sku
        description                = $line.description
        origin                     = $line.origin
        leadTimeWeeks              = $line.leadTimeWeeks
        weeklyForecast             = @($cleansedWeekly)
        frontLoadRequested         = $line.frontLoadRequested
        frontLoadWeeks             = $line.frontLoadWeeks
        historicalActualMonthlyAvg = $line.historicalActualMonthlyAvg
        salesRepConfidence         = $line.salesRepConfidence
        replacementSku             = $line.replacementSku
        replacementEffectiveWeek   = $line.replacementEffectiveWeek
        source                     = $line.source
    }
    Add-Bag $cleansedLines $cLine
}

$cleanseEnvelope = [pscustomobject]@{
    envelope = [pscustomobject]@{
        type = "OSQ_FORECAST_CLEANSED"
        version = "1.0.0"
        timestampUtc = (Get-Date).ToUniversalTime().ToString("o")
        batchId = $raw.batchId
    }
    summary = [pscustomobject]@{
        batchId                 = $raw.batchId
        inputLinesCount         = $acceptedLines.Count
        cleansedLinesCount      = $cleansedLines.Count
        duplicatesEliminated    = $dedupExclusions.Count
        deltasIdentified        = $deltas.Count
        prorationFactorApplied  = $prorationFactor
    }
    cleansedDemandLines    = @($cleansedLines)
    deduplicatedExclusions = @($dedupExclusions)
    deltaAudit             = @($deltas)
}
$fCleanse = Join-Path $resolvedOut "02_forecast_cleansed.json"
Write-Json $fCleanse $cleanseEnvelope

# ---------------- Stage 3: QUIPU Intelligence Worker ----------------
Write-Host "[3/6] Running Stage 3: OSQ QUIPU Intelligence Worker..." -ForegroundColor Green
$quipuLines = New-Bag
$suppressions = New-Bag
$substitutions = New-Bag
$leadTimeRisks = New-Bag
$frontLoadDecisions = New-Bag

foreach ($line in $cleansedLines) {
    $histAvg = [double]$line.historicalActualMonthlyAvg
    $repConf = [double]$line.salesRepConfidence
    $weekly = $line.weeklyForecast
    $origin = [string]$line.origin
    $leadTime = [int]$line.leadTimeWeeks

    $total4w = 0.0
    foreach ($w in $weekly) { if ([int]$w.week -le 4) { $total4w += [double]$w.cleansedQuantity } }
    $inflationRatio = if ($histAvg -gt 0) { $total4w / $histAvg } else { 1.0 }

    # Confidence calculation
    $conf = if ($inflationRatio -gt 1.70) {
        [Math]::Max(0.15, [Math]::Round($repConf * (1.70 / $inflationRatio) * 0.70, 3))
    } else {
        [Math]::Min(0.98, [Math]::Round($repConf * (1.0 - [Math]::Max(0.0, $inflationRatio - 1.0) * 0.2), 3))
    }

    $isOverForecast = ($inflationRatio -gt 1.70)
    $curatedWeekly = New-Bag
    $weeklyHistCap = ($histAvg / 4.0) * 1.40
    $hasSuppression = $false

    foreach ($wf in $weekly) {
        $q = [double]$wf.cleansedQuantity
        if ($isOverForecast -and $q -gt $weeklyHistCap) {
            $hasSuppression = $true
            Add-Bag $curatedWeekly ([pscustomobject]@{
                week = $wf.week
                cleansedQuantity = $q
                curatedQuantity = [Math]::Round($weeklyHistCap, 2)
                suppressionApplied = $true
            })
        } else {
            Add-Bag $curatedWeekly ([pscustomobject]@{
                week = $wf.week
                cleansedQuantity = $q
                curatedQuantity = $q
                suppressionApplied = $false
            })
        }
    }

    if ($hasSuppression) {
        $curated4w = 0.0
        foreach ($cw in $curatedWeekly) { if ([int]$cw.week -le 4) { $curated4w += [double]$cw.curatedQuantity } }
        Add-Bag $suppressions ([pscustomobject]@{
            sku = $line.sku
            quoteId = $line.quoteId
            historicalMonthlyAvg = $histAvg
            demandedMonthlyPace = $total4w
            inflationRatio = [Math]::Round($inflationRatio, 2)
            excessUnitsSuppressed = [Math]::Round($total4w - $curated4w, 2)
            assignedConfidence = $conf
            quipuAction = "SUPPRESSED_OVER_FORECAST"
        })
    }

    # SKU Replacement Handling (No duplicate demand)
    $subLine = $null
    if ($line.replacementSku -and $line.replacementEffectiveWeek) {
        $repW = [int]$line.replacementEffectiveWeek
        $preRep = New-Bag
        $postRep = New-Bag

        foreach ($cw in $curatedWeekly) {
            if ([int]$cw.week -lt $repW) {
                Add-Bag $preRep $cw
            } else {
                # Old SKU zeroed out
                Add-Bag $preRep ([pscustomobject]@{
                    week = $cw.week; cleansedQuantity = $cw.cleansedQuantity; curatedQuantity = 0.0
                    suppressionApplied = $cw.suppressionApplied; skuSubstitutedTo = $line.replacementSku
                })
                # New SKU receives demand
                Add-Bag $postRep ([pscustomobject]@{
                    week = $cw.week; cleansedQuantity = $cw.cleansedQuantity; curatedQuantity = $cw.curatedQuantity
                    suppressionApplied = $cw.suppressionApplied; skuSubstitutedFrom = $line.sku
                })
            }
        }
        $curatedWeekly = $preRep

        $subLine = [pscustomobject]@{
            quoteId                    = $line.quoteId
            lineId                     = "$($line.lineId)-SUB"
            customer                   = $line.customer
            sku                        = $line.replacementSku
            description                = "Auto-substituted replacement for $($line.sku)"
            origin                     = $line.origin
            leadTimeWeeks              = $line.leadTimeWeeks
            weeklyForecast             = @($postRep)
            frontLoadRequested         = $line.frontLoadRequested
            frontLoadWeeks             = $line.frontLoadWeeks
            historicalActualMonthlyAvg = $line.historicalActualMonthlyAvg
            salesRepConfidence         = $line.salesRepConfidence
            quipuConfidenceScore       = $conf
            derivedFromReplacement     = $true
            originalSku                = $line.sku
            source                     = $line.source
            leadTimeSensitivity        = "DOMESTIC_AGILE"
            isOverForecast             = $false
            frontLoadApproved          = $false
        }

        $subQty = 0.0
        foreach ($p in $postRep) { $subQty += [double]$p.curatedQuantity }
        Add-Bag $substitutions ([pscustomobject]@{
            originalSku = $line.sku
            replacementSku = $line.replacementSku
            effectiveWeek = $repW
            substitutedDemandUnits = $subQty
        })
    }

    # Lead-Time Sensitivity
    $leadSensitivity = "LOW_RISK"
    if ($origin -eq 'IMPORT' -and $leadTime -ge 12) {
        $leadSensitivity = "CRITICAL_IMPORT_SENSITIVITY"
        Add-Bag $leadTimeRisks ([pscustomobject]@{
            sku = $line.sku
            origin = $origin
            leadTimeWeeks = $leadTime
            planningHorizonWeeks = 26
            riskLevel = "HIGH"
            recommendation = "Import lead time ($leadTime wks) consumes $([Math]::Round(($leadTime/26.0)*100))% of Blue Yonder horizon. Safety stock buffer advised in Blue Yonder."
        })
    } elseif ($origin -eq 'DOMESTIC') {
        $leadSensitivity = "DOMESTIC_AGILE"
    }

    # Front-Loading Decision
    $frontApproved = $false
    if ($line.frontLoadRequested -and [int]$line.frontLoadWeeks -gt 0) {
        if ($conf -ge 0.65 -and -not $isOverForecast) {
            $frontApproved = $true
            Add-Bag $frontLoadDecisions ([pscustomobject]@{
                sku = $line.sku; frontLoadWeeks = $line.frontLoadWeeks; status = "APPROVED"
                reason = "High confidence and verified historical baseline permit front-loading."
            })
        } else {
            $frontApproved = $false
            Add-Bag $frontLoadDecisions ([pscustomobject]@{
                sku = $line.sku; frontLoadWeeks = $line.frontLoadWeeks; status = "BLOCKED_BY_QUIPU"
                reason = "Front-loading denied: forecast inflation or low confidence creates inventory trap."
            })
        }
    }

    $curatedLine = [pscustomobject]@{
        quoteId                    = $line.quoteId
        lineId                     = $line.lineId
        customer                   = $line.customer
        sku                        = $line.sku
        description                = $line.description
        origin                     = $line.origin
        leadTimeWeeks              = $line.leadTimeWeeks
        weeklyForecast             = @($curatedWeekly)
        frontLoadRequested         = $line.frontLoadRequested
        frontLoadWeeks             = $line.frontLoadWeeks
        historicalActualMonthlyAvg = $line.historicalActualMonthlyAvg
        salesRepConfidence         = $line.salesRepConfidence
        quipuConfidenceScore       = $conf
        leadTimeSensitivity        = $leadSensitivity
        isOverForecast             = $isOverForecast
        frontLoadApproved          = $frontApproved
    }
    Add-Bag $quipuLines $curatedLine
    if ($subLine) { Add-Bag $quipuLines $subLine }
}

$quipuEnvelope = [pscustomobject]@{
    envelope = [pscustomobject]@{
        type = "OSQ_QUIPU_INTELLIGENCE_CURATED"
        version = "1.0.0"
        timestampUtc = (Get-Date).ToUniversalTime().ToString("o")
        batchId = $raw.batchId
        intelligenceProvider = "QUIPU"
    }
    summary = [pscustomobject]@{
        batchId                   = $raw.batchId
        totalLinesEvaluated       = $cleansedLines.Count
        totalCuratedLinesOutput   = $quipuLines.Count
        anomaliesSuppressedCount  = $suppressions.Count
        skuSubstitutionsCount     = $substitutions.Count
        importRiskLinesCount      = $leadTimeRisks.Count
        frontLoadDecisionsCount   = $frontLoadDecisions.Count
        quipuObserverConnected    = $false
    }
    curatedDemandLines          = @($quipuLines)
    suppressedAnomalies         = @($suppressions)
    skuSubstitutions            = @($substitutions)
    leadTimeSensitivityAnalysis = @($leadTimeRisks)
    frontLoadingDecisions       = @($frontLoadDecisions)
}
$fQuipu = Join-Path $resolvedOut "03_quipu_curated.json"
Write-Json $fQuipu $quipuEnvelope

# ---------------- Stage 4: Governance Gate & Emergency Brake ----------------
Write-Host "[4/6] Running Stage 4: OSQ Forecast Governance Gate & Emergency Brake..." -ForegroundColor Green
$confMin = if ($Mode -eq 'strict') { 0.40 } elseif ($Mode -eq 'adaptive') { 0.25 } else { 0.10 }
$maxImportUnits = if ($Mode -eq 'strict') { 500 } else { 1500 }
$brakeTripped = $false
$brakeReasons = New-Bag
$approvalReqs = New-Bag
$approvedLines = New-Bag
$heldLines = New-Bag

foreach ($line in $quipuLines) {
    $conf = [double]$line.quipuConfidenceScore
    $units = 0.0
    foreach ($w in $line.weeklyForecast) { $units += [double]$w.curatedQuantity }

    if ($conf -lt $confMin) {
        $brakeTripped = $true
        $r = "EMERGENCY BRAKE: SKU $($line.sku) (Quote $($line.quoteId)) confidence $conf is below threshold $confMin."
        Add-Bag $brakeReasons $r
        Add-Bag $approvalReqs ([pscustomobject]@{
            approvalId = "APPR-$($raw.batchId)-$($line.sku)"
            sku = $line.sku; quoteId = $line.quoteId; type = "LOW_CONFIDENCE_EXCEPTION"
            confidence = $conf; status = "PENDING_PLANNER_REVIEW"
            recommendedAction = "Hold demand publication until sales verification."
        })
        Add-Bag $heldLines $line
        continue
    }

    if ($line.origin -eq 'IMPORT' -and $units -gt $maxImportUnits) {
        $brakeTripped = $true
        $r = "EMERGENCY BRAKE: Import SKU $($line.sku) requires $units units exceeding unapproved cap ($maxImportUnits)."
        Add-Bag $brakeReasons $r
        Add-Bag $approvalReqs ([pscustomobject]@{
            approvalId = "APPR-$($raw.batchId)-$($line.sku)-IMPORT"
            sku = $line.sku; quoteId = $line.quoteId; type = "HIGH_VOLUME_IMPORT_EXCEPTION"
            volume = $units; status = "PENDING_SUPPLY_CHAIN_APPROVAL"
            recommendedAction = "Confirm supplier container allocation before publishing to Blue Yonder."
        })
        Add-Bag $heldLines $line
        continue
    }

    Add-Bag $approvedLines $line
}

$govEnvelope = [pscustomobject]@{
    envelope = [pscustomobject]@{
        type = "OSQ_GOVERNANCE_DECISION"
        version = "1.0.0"
        timestampUtc = (Get-Date).ToUniversalTime().ToString("o")
        batchId = $raw.batchId
        governanceMode = $Mode
        state = if ($brakeTripped) { "PARTIALLY_APPROVED" } else { "ACCEPTED" }
    }
    summary = [pscustomobject]@{
        batchId                = $raw.batchId
        governanceMode         = $Mode
        emergencyBrakeTripped  = $brakeTripped
        totalLinesEvaluated    = $quipuLines.Count
        linesApproved          = $approvedLines.Count
        linesHeldForApproval   = $heldLines.Count
        approvalRequestsCount  = $approvalReqs.Count
    }
    approvedDemandLines     = @($approvedLines)
    heldDemandLines         = @($heldLines)
    emergencyBrakeReasons   = @($brakeReasons)
    pendingApprovalRequests = @($approvalReqs)
}
$fGov = Join-Path $resolvedOut "04_governance_decision.json"
Write-Json $fGov $govEnvelope

# ---------------- Stage 5: Blue Yonder Transformation Worker ----------------
Write-Host "[5/6] Running Stage 5: OSQ Blue Yonder Transformation Worker..." -ForegroundColor Green
$byBuckets = @{}
$byMeta = @{}
$frontLoadAudit = New-Bag

foreach ($line in $approvedLines) {
    $sku = [string]$line.sku
    if (-not $byBuckets.ContainsKey($sku)) {
        $byBuckets[$sku] = @{}
        for ($w = 1; $w -le 26; $w++) { $byBuckets[$sku]["W$("{0:D2}" -f $w)"] = 0.0 }
        $byMeta[$sku] = [pscustomobject]@{
            sku = $sku
            description = $line.description
            origin = $line.origin
            leadTimeWeeks = $line.leadTimeWeeks
            confidenceScore = $line.quipuConfidenceScore
        }
    }

    $frontLoadApproved = [bool]$line.frontLoadApproved
    $flWeeks = [int]$line.frontLoadWeeks

    foreach ($wf in $line.weeklyForecast) {
        $origW = [int]$wf.week
        $q = [double]$wf.curatedQuantity
        $targetW = if ($frontLoadApproved -and $flWeeks -gt 0) { [Math]::Max(1, $origW - $flWeeks) } else { $origW }

        if ($targetW -ge 1 -and $targetW -le 26) {
            $key = "W$("{0:D2}" -f $targetW)"
            $byBuckets[$sku][$key] = [Math]::Round($byBuckets[$sku][$key] + $q, 2)
        }

        if ($frontLoadApproved -and $flWeeks -gt 0) {
            Add-Bag $frontLoadAudit ([pscustomobject]@{
                sku = $sku; quoteId = $line.quoteId; originalWeek = $origW; targetWeek = $targetW
                shiftedQuantity = $q; leadTimeWeeks = $line.leadTimeWeeks
            })
        }
    }
}

$byPublication = New-Bag
$totalPubUnits = 0.0
foreach ($sku in ($byBuckets.Keys | Sort-Object)) {
    $m = $byMeta[$sku]
    $skuTotal = 0.0
    foreach ($wKey in $byBuckets[$sku].Keys) { $skuTotal += [double]$byBuckets[$sku][$wKey] }
    $totalPubUnits += $skuTotal

    Add-Bag $byPublication ([pscustomobject]@{
        sku                     = $sku
        description             = $m.description
        origin                  = $m.origin
        leadTimeWeeks           = $m.leadTimeWeeks
        confidenceScore         = $m.confidenceScore
        total6MonthHorizonUnits = [Math]::Round($skuTotal, 2)
        weeklyBuckets           = $byBuckets[$sku]
    })
}

$byEnvelope = [pscustomobject]@{
    envelope = [pscustomobject]@{
        type = "OSQ_BLUE_YONDER_TRANSFORMED"
        version = "1.0.0"
        timestampUtc = (Get-Date).ToUniversalTime().ToString("o")
        batchId = $raw.batchId
        targetSystem = "BLUE_YONDER"
    }
    summary = [pscustomobject]@{
        batchId                = $raw.batchId
        planningHorizonWeeks   = 26
        planningHorizonMonths  = 6
        uniqueSkusPublished    = $byPublication.Count
        totalUnitsPublished    = [Math]::Round($totalPubUnits, 2)
        frontLoadedLinesCount  = $frontLoadAudit.Count
        targetEngine           = "Blue Yonder Demand / Replenishment"
    }
    blueYonderDemandPublication = @($byPublication)
    frontLoadingAudit           = @($frontLoadAudit)
}
$fBy = Join-Path $resolvedOut "05_blue_yonder_transformed.json"
Write-Json $fBy $byEnvelope

# ---------------- Stage 6: Integration Connectors ----------------
Write-Host "[6/6] Running Stage 6: OSQ Integration Connectors (BY & SAP)..." -ForegroundColor Green
$byRecords = New-Bag
$sapReservations = New-Bag

foreach ($item in $byPublication) {
    $sku = [string]$item.sku
    $lt = [int]$item.leadTimeWeeks
    $conf = [double]$item.confidenceScore

    foreach ($key in ($item.weeklyBuckets.Keys | Sort-Object)) {
        $q = [double]$item.weeklyBuckets[$key]
        if ($q -gt 0) {
            Add-Bag $byRecords ([pscustomobject]@{
                recordType       = "WEEKLY_DEMAND"
                plantId          = $Plant
                itemNumber       = $sku
                planningBucket   = $key
                quantity         = $q
                confidenceFactor = $conf
                leadTimeWeeks    = $lt
                sourceHolon      = "x_osq"
            })
        }
    }

    # SAP near-term reservation for project demand (W01-W04)
    $nearTermUnits = 0.0
    for ($w = 1; $w -le 4; $w++) {
        $k = "W$("{0:D2}" -f $w)"
        if ($item.weeklyBuckets.ContainsKey($k)) {
            $nearTermUnits += [double]$item.weeklyBuckets[$k]
        }
    }

    if ($nearTermUnits -gt 0) {
        Add-Bag $sapReservations ([pscustomobject]@{
            reservationNumber = "RES-SAP-$($raw.batchId.Substring(6))-$sku"
            plant             = $Plant
            storageLocation   = "0001"
            materialNumber    = $sku
            reservedQuantity  = [Math]::Round($nearTermUnits, 2)
            requirementDate   = (Get-Date).AddDays(7).ToString("yyyy-MM-dd")
            movementType      = "261"
            commitmentSource  = "CPQ_ACCEPTED_QUOTE"
        })
    }
}

$syncEnvelope = [pscustomobject]@{
    envelope = [pscustomobject]@{
        type = "OSQ_CONNECTOR_SYNC_MANIFEST"
        version = "1.0.0"
        timestampUtc = (Get-Date).ToUniversalTime().ToString("o")
        batchId = $raw.batchId
        connectors = @("BLUE_YONDER", "SAP_RESERVATIONS")
    }
    summary = [pscustomobject]@{
        batchId                = $raw.batchId
        plantId                = $Plant
        blueYonderRecordsCount = $byRecords.Count
        sapReservationsCount   = $sapReservations.Count
        status                 = "READY_FOR_PUBLICATION"
    }
    blueYonderConnectorExport      = @($byRecords)
    sapReservationSynchronization  = @($sapReservations)
}
$fSync = Join-Path $resolvedOut "06_connector_sync_manifest.json"
Write-Json $fSync $syncEnvelope

# ---------------- Master HubCore Envelope ----------------
$masterEnvelope = [pscustomobject]@{
    task = [pscustomobject]@{
        taskId       = "task-$([Guid]::NewGuid().ToString('N').Substring(0,12))"
        source       = "x_hubcore"
        target       = "x_osq"
        operation    = "run-all"
        state        = "COMPLETED"
        timestampUtc = (Get-Date).ToUniversalTime().ToString("o")
    }
    result = [pscustomobject]@{
        producer = "x_osq"
        status   = "SUCCESS"
        summary  = [pscustomobject]@{
            inputQuotesReceived        = @($raw.quotes).Count
            acceptedQuotes             = @($raw.quotes | Where-Object { $_.status -eq 'ACCEPTED' }).Count
            ineligibleQuotesFiltered   = @($raw.quotes | Where-Object { $_.status -ne 'ACCEPTED' }).Count
            duplicatesEliminated       = $dedupExclusions.Count
            deltasTracked              = $deltas.Count
            quipuOverForecastSuppressed= $suppressions.Count
            skuSubstitutionsExecuted   = $substitutions.Count
            emergencyBrakeTripped      = $brakeTripped
            linesApproved              = $approvedLines.Count
            linesHeld                  = $heldLines.Count
            blueYonderSkusPublished    = $byPublication.Count
            blueYonderTotalUnits       = [Math]::Round($totalPubUnits, 2)
            sapReservationsCreated     = $sapReservations.Count
        }
    }
    event = [pscustomobject]@{
        eventId      = "evt-$([Guid]::NewGuid().ToString('N').Substring(0,12))"
        type         = "osq.forecast_pipeline.completed"
        producer     = "x_osq"
        timestampUtc = (Get-Date).ToUniversalTime().ToString("o")
    }
    artifacts = @(
        $fIntake, $fCleanse, $fQuipu, $fGov, $fBy, $fSync
    )
}
$fMaster = Join-Path $resolvedOut "00_osq_master_envelope.json"
Write-Json $fMaster $masterEnvelope

Write-Host "=== Master Envelope Generated: $fMaster ===" -ForegroundColor Green
$masterEnvelope | ConvertTo-Json -Depth 6
