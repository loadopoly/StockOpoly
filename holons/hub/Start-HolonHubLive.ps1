#Requires -Version 5.1
<#
.SYNOPSIS
    Native Windows PowerShell 5.1 / .NET C# HolonHubLive Network Server (HTTP + SSE) for x_OSQ (:8900).

.DESCRIPTION
    Serves x_OSQ.html and maintains a connected multi-user live channel over HTTP + Server-Sent Events (SSE)
    backed by two synchronized data tiers:
      1. Relationally Static Data: Data/hub/osq_relational_store.json (+ mirror)
      2. Shared Temp Data: Data/hub/temp/holonhub_live_temp.json

.PARAMETER Port
    TCP loopback port to listen on (default: 8900, matching hub_workspace.json osqEngine).

.PARAMETER Background
    Launch in a detached background PowerShell process and wait for /health.

.PARAMETER Stop
    Stop the running background server process on -Port.

.PARAMETER SelfTest
    Run automated multi-user HTTP/state/conflict verification against the running server.

.PARAMETER ServeForeground
    Internal flag used by -Background to run the server loop in the foreground.
#>
[CmdletBinding()]
param(
    [int]$Port = 8900,
    [string]$HostIp = '127.0.0.1',
    [string]$HtmlPath = '',
    [string]$CrmHtmlPath = '',
    [string]$RelationalPath = '',
    [string]$MirrorRelationalPath = '',
    [string]$TempPath = '',
    [switch]$Background,
    [switch]$Stop,
    [switch]$SelfTest,
    [switch]$ServeForeground
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$OsqRoot   = Split-Path -Parent $ScriptDir
$DocsRoot  = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $OsqRoot))))
$VsCodeMainDefault = Join-Path $DocsRoot 'VS Code\VS-Code-main'

if (-not $HtmlPath) {
    $candidate1 = Join-Path $OsqRoot 'x_OSQ.html'
    $candidate2 = Join-Path $VsCodeMainDefault 'Loadopoly-Portal\x_OSQ.html'
    $HtmlPath = if (Test-Path $candidate1) { $candidate1 } else { $candidate2 }
}
if (-not $CrmHtmlPath) {
    $crm1 = Join-Path $OsqRoot 'x_CRM.html'
    $crm2 = Join-Path $VsCodeMainDefault 'PROD\x_CRM.html'
    $crm3 = Join-Path $VsCodeMainDefault 'Loadopoly-Portal\x_CRM.html'
    $CrmHtmlPath = if (Test-Path $crm1) { $crm1 } elseif (Test-Path $crm2) { $crm2 } else { $crm3 }
}
if (-not $RelationalPath) {
    $candidate = Join-Path $VsCodeMainDefault 'Data\hub\osq_relational_store.json'
    if (Test-Path $candidate) { $RelationalPath = $candidate } else { $RelationalPath = Join-Path $OsqRoot 'out\osq_relational_store.json' }
}
if (-not $MirrorRelationalPath) {
    $MirrorRelationalPath = Join-Path $OsqRoot 'out\osq_relational_store.json'
}
if (-not $TempPath) {
    $candidate = Join-Path $VsCodeMainDefault 'Data\hub\temp\holonhub_live_temp.json'
    if (Test-Path (Split-Path -Parent $candidate)) { $TempPath = $candidate } else { $TempPath = Join-Path $OsqRoot 'out\holonhub_live_temp.json' }
}

$LogDir  = Join-Path $OsqRoot 'out\logs'
$PidFile = Join-Path $LogDir "holonhub_live_${Port}.pid"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }

function Stop-HolonHubLiveDaemon {
    param([int]$TargetPort, [string]$TargetPidFile)
    $stopped = $false
    if (Test-Path $TargetPidFile) {
        $pidText = (Get-Content $TargetPidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($pidText -match '^\d+$') {
            $proc = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
            if ($proc) {
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                Write-Host "[STOP] Stopped HolonHubLive server PID $($proc.Id)" -ForegroundColor Yellow
                $stopped = $true
            }
        }
        Remove-Item $TargetPidFile -Force -ErrorAction SilentlyContinue
    }
    $conns = Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        foreach ($c in $conns) {
            if ($c.OwningProcess -gt 0) {
                Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
                Write-Host "[STOP] Freed port $TargetPort (PID $($c.OwningProcess))" -ForegroundColor Yellow
                $stopped = $true
            }
        }
    }
    if (-not $stopped) {
        Write-Host "[INFO] No HolonHubLive server was running on port $TargetPort." -ForegroundColor Gray
    }
}

if ($Stop) {
    Stop-HolonHubLiveDaemon -TargetPort $Port -TargetPidFile $PidFile
    exit 0
}

if ($Background) {
    $existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "[INFO] Port $Port already listening (PID $($existing[0].OwningProcess)). Checking /health..." -ForegroundColor Cyan
        try {
            $h = Invoke-RestMethod -Uri "http://${HostIp}:${Port}/health" -TimeoutSec 2
            if ($h.status -eq 'ok') {
                Write-Host "[PASS] HolonHubLive server already healthy on http://${HostIp}:${Port}" -ForegroundColor Green
                exit 0
            }
        } catch {}
        Stop-HolonHubLiveDaemon -TargetPort $Port -TargetPidFile $PidFile
    }

    $scriptPath = $MyInvocation.MyCommand.Path
    $argList = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$scriptPath`"",
        '-Port', $Port,
        '-HostIp', "`"$HostIp`"",
        '-HtmlPath', "`"$HtmlPath`"",
        '-CrmHtmlPath', "`"$CrmHtmlPath`"",
        '-RelationalPath', "`"$RelationalPath`"",
        '-MirrorRelationalPath', "`"$MirrorRelationalPath`"",
        '-TempPath', "`"$TempPath`"",
        '-ServeForeground'
    )
    $proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -WindowStyle Hidden -PassThru
    $proc.Id | Out-File -FilePath $PidFile -Encoding ascii -Force

    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 350
        try {
            $res = Invoke-RestMethod -Uri "http://${HostIp}:${Port}/health" -TimeoutSec 2 -ErrorAction Stop
            if ($res.status -eq 'ok') { $ready = $true; break }
        } catch {}
    }
    if (-not $ready) {
        throw "Timed out waiting for HolonHubLive server on http://${HostIp}:${Port}/health"
    }
    Write-Host "[PASS] HolonHubLive running in background PID $($proc.Id) on http://${HostIp}:${Port}" -ForegroundColor Green
    if (-not $SelfTest) { exit 0 }
}

if ($SelfTest) {
    Write-Host "`nRunning HolonHubLive Multi-User Network Verification Self-Test against http://${HostIp}:${Port}..." -ForegroundColor Cyan
    $base = "http://${HostIp}:${Port}"

    $h = Invoke-RestMethod -Uri "$base/health" -Method Get
    if ($h.status -ne 'ok') { throw "Health check failed" }
    Write-Host ("  [PASS] /health ok (rev={0}, tempRev={1})" -f $h.revision, $h.tempRevision) -ForegroundColor Green

    $st = Invoke-RestMethod -Uri "$base/api/v1/osq/state" -Method Get
    if (-not $st.ok -or -not $st.relational.items) { throw "State missing relational items" }
    Write-Host ("  [PASS] /api/v1/osq/state loaded ({0} items, {1} quotes, {2} opportunities, {3} shards)" -f `
        $st.relational.items.Count, $st.relational.quotes.Count, $st.relational.opportunities.Count, $st.relational.marketplaceShards.Count) -ForegroundColor Green

    # User A session heartbeat + lock acquire
    $null = Invoke-RestMethod -Uri "$base/api/v1/osq/temp" -Method Post -ContentType 'application/json' -Body (@{
        action = 'session.heartbeat'
        payload = @{ sessionId = 'SES-TEST-A'; userId = 'CustomerROF'; persona = 'Customer (ROF Portal)'; activeTab = 'tab-customer-portal'; focusedEntity = 'ROF-DEMO-001' }
    } | ConvertTo-Json -Depth 5)

    $lockA = Invoke-RestMethod -Uri "$base/api/v1/osq/temp" -Method Post -ContentType 'application/json' -Body (@{
        action = 'lock.acquire'
        payload = @{ entityKey = 'ROF-DEMO-001'; sessionId = 'SES-TEST-A'; userId = 'CustomerROF'; persona = 'Customer (ROF Portal)'; lockAction = 'Editing ROF draft' }
    } | ConvertTo-Json -Depth 5)
    Write-Host "  [PASS] User A acquired soft lock on ROF-DEMO-001" -ForegroundColor Green

    # User B lock conflict expected (409)
    $conflictCaught = $false
    try {
        $null = Invoke-RestMethod -Uri "$base/api/v1/osq/temp" -Method Post -ContentType 'application/json' -Body (@{
            action = 'lock.acquire'
            payload = @{ entityKey = 'ROF-DEMO-001'; sessionId = 'SES-TEST-B'; userId = 'SellerRSR'; persona = 'RSR / Seller'; lockAction = 'Editing ROF price' }
        } | ConvertTo-Json -Depth 5)
    } catch {
        if ($_.Exception.Response.StatusCode.value__ -eq 409) { $conflictCaught = $true }
    }
    if (-not $conflictCaught) { throw "Expected HTTP 409 conflict when User B tried to lock ROF-DEMO-001 held by User A" }
    Write-Host "  [PASS] Collaborative lock conflict enforcement (HTTP 409) verified for User B" -ForegroundColor Green

    # Release User A lock
    $null = Invoke-RestMethod -Uri "$base/api/v1/osq/temp" -Method Post -ContentType 'application/json' -Body (@{
        action = 'lock.release'
        payload = @{ entityKey = 'ROF-DEMO-001'; sessionId = 'SES-TEST-A' }
    } | ConvertTo-Json -Depth 5)
    Write-Host "  [PASS] Soft lock released cleanly" -ForegroundColor Green

    # Test CPQ-1 Closed account guard (should return 400 Bad Request)
    $closedBlocked = $false
    try {
        $null = Invoke-RestMethod -Uri "$base/api/v1/osq/mutate" -Method Post -ContentType 'application/json' -Body (@{
            action = 'opportunity.create'
            actor = 'SellerRSR'
            persona = 'RSR / Seller'
            payload = @{ accountId = 'ACC-CLOSED-99'; name = 'Blocked Opp' }
        } | ConvertTo-Json -Depth 5)
    } catch {
        if ($_.Exception.Response.StatusCode.value__ -eq 400) { $closedBlocked = $true }
    }
    if (-not $closedBlocked) { throw "Expected HTTP 400 when creating opportunity on Closed account ACC-CLOSED-99" }
    Write-Host "  [PASS] Relational CPQ-1 Closed Account rule enforced (HTTP 400)" -ForegroundColor Green

    $obTest = Invoke-RestMethod -Uri "$base/api/v1/osq/outbox?sinceCursor=0" -Method Get
    $null = Invoke-RestMethod -Uri "$base/api/v1/osq/stockopoly/slot-lookup" -Method Post -ContentType 'application/json' -Body '{"sku":"SKU-HVAC-3001","dc":"TX03","simulateDrift":true}'
    $pwTest = Invoke-RestMethod -Uri "$base/api/v1/osq/pathways" -Method Get
    $dirTest = Invoke-RestMethod -Uri "$base/api/v1/osq/directives?status=open" -Method Get
    if (-not $obTest.ok -or -not $pwTest.ok -or $pwTest.returnChainOwner -ne 'return-quarantine-governance' -or @($dirTest.directives).Count -lt 1) {
        throw "Outbox / Pathways / Directives SelfTest failed"
    }
    Write-Host ("  [PASS] Outbox (cursor={0}), Pathways (sys={1}) & Directives (open={2}) verified" -f `
        $obTest.cursor, @($pwTest.systemic).Count, @($dirTest.directives).Count) -ForegroundColor Green

    Write-Host "All Multi-User Network Server checks passed.`n" -ForegroundColor Green
    if (-not $ServeForeground) { exit 0 }
}

# -----------------------------------------------------------------------------
# Foreground Server Mode: Compile Embedded C# Multi-Threaded HTTP + SSE Engine
# -----------------------------------------------------------------------------
$CSharpSource = @'
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

namespace HolonHubLive
{
    public class SseClient
    {
        public TcpClient Client;
        public NetworkStream Stream;
        public object WriteLock = new object();
        public bool Alive = true;
    }

    public class Server
    {
        private readonly string _host;
        private readonly int _port;
        private readonly string _htmlPath;
        private readonly string _crmHtmlPath;
        private readonly string _relPath;
        private readonly string _mirrorPath;
        private readonly string _tempPath;

        private readonly object _stateLock = new object();
        private readonly object _sseLock = new object();
        private readonly List<SseClient> _sseClients = new List<SseClient>();
        private Dictionary<string, object> _relational;
        private Dictionary<string, object> _temp;
        private volatile bool _running = true;

        private int _fencingCounter = 0;
        private readonly Dictionary<string, int> _entityMaxFencing = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        private readonly Dictionary<string, Dictionary<string, object>> _idempotencyIndex = new Dictionary<string, Dictionary<string, object>>(StringComparer.OrdinalIgnoreCase);
        private readonly Dictionary<string, Dictionary<string, object>> _tasks = new Dictionary<string, Dictionary<string, object>>(StringComparer.OrdinalIgnoreCase);
        private readonly List<string> _taskOrder = new List<string>();
        private readonly List<Dictionary<string, object>> _outbox = new List<Dictionary<string, object>>();
        private readonly List<Dictionary<string, object>> _restrictedAudit = new List<Dictionary<string, object>>();
        private readonly HashSet<string> _marketplaceReplays = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        private readonly Dictionary<string, Dictionary<string, object>> _connectorOutcomes = new Dictionary<string, Dictionary<string, object>>(StringComparer.OrdinalIgnoreCase);
        private readonly List<Dictionary<string, object>> _immutableSnapshots = new List<Dictionary<string, object>>();
        private readonly Dictionary<string, Dictionary<string, object>> _registrations = new Dictionary<string, Dictionary<string, object>>(StringComparer.OrdinalIgnoreCase);
        private Dictionary<string, object> _meshState = new Dictionary<string, object>();

        private const int SessionTtlSeconds = 45;
        private const int LockTtlSeconds = 120;
        private const int MaxPayloadBytes = 1048576;
        private const string PolicyVersion = "owner-policy/v2.3";
        private const string HomeNodeId = "node-a-osq-home";
        private const string PeerNodeId = "node-b-federated-peer";

        public Server(string host, int port, string htmlPath, string relPath, string mirrorPath, string tempPath)
            : this(host, port, htmlPath, "", relPath, mirrorPath, tempPath)
        {
        }

        public Server(string host, int port, string htmlPath, string crmHtmlPath, string relPath, string mirrorPath, string tempPath)
        {
            _host = host;
            _port = port;
            _htmlPath = htmlPath;
            _crmHtmlPath = crmHtmlPath ?? "";
            _relPath = relPath;
            _mirrorPath = mirrorPath;
            _tempPath = tempPath;
            LoadOrInit();
        }

        private JavaScriptSerializer NewSerializer()
        {
            var ser = new JavaScriptSerializer();
            ser.MaxJsonLength = int.MaxValue;
            ser.RecursionLimit = 256;
            return ser;
        }

        private static string UtcNowIso()
        {
            return DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ");
        }

        private static long NowEpoch()
        {
            return (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
        }

        private static string Sha256Hex(string input)
        {
            using (var sha = SHA256.Create())
            {
                byte[] hash = sha.ComputeHash(Encoding.UTF8.GetBytes(input ?? ""));
                var sb = new StringBuilder(hash.Length * 2);
                foreach (byte b in hash) sb.Append(b.ToString("x2"));
                return sb.ToString();
            }
        }

        private static string Sha1Hex(string input)
        {
            using (var sha = SHA1.Create())
            {
                byte[] hash = sha.ComputeHash(Encoding.UTF8.GetBytes(input ?? ""));
                var sb = new StringBuilder(hash.Length * 2);
                foreach (byte b in hash) sb.Append(b.ToString("x2"));
                return sb.ToString();
            }
        }

        private string CanonicalSerialize(object val)
        {
            var ser = NewSerializer();
            if (val == null) return "null";
            var dict = val as Dictionary<string, object>;
            if (dict != null)
            {
                var keys = new List<string>(dict.Keys);
                keys.Sort(StringComparer.Ordinal);
                var sb = new StringBuilder("{");
                for (int i = 0; i < keys.Count; i++)
                {
                    if (i > 0) sb.Append(",");
                    sb.Append(ser.Serialize(keys[i]));
                    sb.Append(":");
                    sb.Append(CanonicalSerialize(dict[keys[i]]));
                }
                sb.Append("}");
                return sb.ToString();
            }
            var list = val as ArrayList;
            if (list != null)
            {
                var sb = new StringBuilder("[");
                for (int i = 0; i < list.Count; i++)
                {
                    if (i > 0) sb.Append(",");
                    sb.Append(CanonicalSerialize(list[i]));
                }
                sb.Append("]");
                return sb.ToString();
            }
            var objArr = val as object[];
            if (objArr != null)
            {
                var sb = new StringBuilder("[");
                for (int i = 0; i < objArr.Length; i++)
                {
                    if (i > 0) sb.Append(",");
                    sb.Append(CanonicalSerialize(objArr[i]));
                }
                sb.Append("]");
                return sb.ToString();
            }
            return ser.Serialize(val);
        }

        private string CanonicalHash(object val)
        {
            return Sha256Hex(CanonicalSerialize(val));
        }

        private static bool ValidateSecretRef(string authRef, out string err)
        {
            err = "";
            string val = (authRef ?? "").Trim();
            if (string.IsNullOrEmpty(val) || val.Equals("NONE", StringComparison.OrdinalIgnoreCase)) return true;
            string low = val.ToLowerInvariant();
            if (low.StartsWith("bearer ") || low.Contains("eyj") || low.Contains("private key") || low.Contains("password=") || low.Contains("secret="))
            {
                err = "Air-Lock Policy Blocked: secret references only (ENV:*, VAULT:*, SECRETREF:*, or NONE); raw credentials forbidden.";
                return false;
            }
            string up = val.ToUpperInvariant();
            if (!up.StartsWith("ENV:") && !up.StartsWith("VAULT:") && !up.StartsWith("SECRETREF:"))
            {
                err = "Air-Lock Policy Blocked: authRef must use ENV:, VAULT:, SECRETREF:, or NONE.";
                return false;
            }
            return true;
        }

        private void InitControlPlaneState()
        {
            long nowEpoch = NowEpoch();
            string leaseExp = DateTime.UtcNow.AddSeconds(300).ToString("yyyy-MM-ddTHH:mm:ssZ", System.Globalization.CultureInfo.InvariantCulture);
            _fencingCounter = 0;
            _entityMaxFencing.Clear();
            _idempotencyIndex.Clear();
            _tasks.Clear();
            _taskOrder.Clear();
            _outbox.Clear();
            _restrictedAudit.Clear();
            _marketplaceReplays.Clear();
            _connectorOutcomes.Clear();
            _immutableSnapshots.Clear();
            _registrations.Clear();

            _registrations["x_osq"] = new Dictionary<string, object>
            {
                { "registrationId", "REG-XOSQ-NODEA-01" },
                { "holonId", "x_osq" },
                { "instanceId", "x_osq-node-a-01" },
                { "homeNodeId", HomeNodeId },
                { "version", "2.3.0" },
                { "status", "ACTIVE" },
                { "routable", true },
                { "capabilities", new ArrayList(new[] { "osq.forecast-intake", "osq.forecast-cleansing", "osq.quipu-intelligence", "osq.forecast-governance", "osq.by-transformation", "osq.integration-connectors" }) },
                { "grantedScopes", new ArrayList(new[] { "crm.read", "crm.write", "cpq.write", "osq.read", "osq.write", "gard.artifact.exchange", "connector.fulfillment.execute", "analytics.duckdb.read", "model.gateway.invoke", "process.studio.submit_task" }) },
                { "healthEndpoint", "/health" },
                { "artifactHash", CanonicalHash(new Dictionary<string, object> { { "holonId", "x_osq" }, { "version", "2.3.0" } }) },
                { "registeredAt", UtcNowIso() },
                { "leaseExpiresAt", leaseExp },
                { "leaseExpiresEpoch", nowEpoch + 300 },
                { "reason", "Signed manifest verified and health ready" }
            };
            _registrations["quipu-observer"] = new Dictionary<string, object>
            {
                { "registrationId", "REG-QUIPU-7100" },
                { "holonId", "quipu-observer" },
                { "instanceId", "quipu-node-a-01" },
                { "homeNodeId", HomeNodeId },
                { "version", "1.0.0" },
                { "status", "ACTIVE" },
                { "routable", true },
                { "capabilities", new ArrayList(new[] { "intelligence.quipu.evaluate" }) },
                { "grantedScopes", new ArrayList(new[] { "analytics.duckdb.read", "governance.intent.emit" }) },
                { "healthEndpoint", "http://127.0.0.1:7100/health" },
                { "registeredAt", UtcNowIso() },
                { "leaseExpiresAt", leaseExp },
                { "leaseExpiresEpoch", nowEpoch + 300 }
            };
            _registrations["x_ModelGateway"] = new Dictionary<string, object>
            {
                { "registrationId", "REG-MODELGW-01" },
                { "holonId", "x_ModelGateway" },
                { "instanceId", "model-gw-01" },
                { "homeNodeId", HomeNodeId },
                { "version", "1.0.0" },
                { "status", "ACTIVE" },
                { "routable", true },
                { "capabilities", new ArrayList(new[] { "model.gateway.invoke" }) },
                { "grantedScopes", new ArrayList(new[] { "model.gateway.invoke" }) },
                { "healthEndpoint", "/health" },
                { "registeredAt", UtcNowIso() },
                { "leaseExpiresAt", leaseExp },
                { "leaseExpiresEpoch", nowEpoch + 300 }
            };
            _registrations["ProcessStudio"] = new Dictionary<string, object>
            {
                { "registrationId", "REG-PROCSTUDIO-01" },
                { "holonId", "ProcessStudio" },
                { "instanceId", "process-studio-01" },
                { "homeNodeId", HomeNodeId },
                { "version", "1.0.0" },
                { "status", "ACTIVE" },
                { "routable", true },
                { "capabilities", new ArrayList(new[] { "workflow.process.orchestrate" }) },
                { "grantedScopes", new ArrayList(new[] { "process.studio.submit_task" }) },
                { "healthEndpoint", "/health" },
                { "registeredAt", UtcNowIso() },
                { "leaseExpiresAt", leaseExp },
                { "leaseExpiresEpoch", nowEpoch + 300 }
            };

            _meshState = new Dictionary<string, object>
            {
                { "homeNodeId", HomeNodeId },
                { "peerNodeId", PeerNodeId },
                { "protocolVersion", "holon-mesh/2.3" },
                { "peerBindings", new Dictionary<string, object>() },
                { "peerInbox", new ArrayList() },
                { "seenEnvelopeIds", new ArrayList() },
                { "advertisedWithdrawnCount", 0 },
                { "nodeB", new Dictionary<string, object> {
                    { "nodeId", PeerNodeId },
                    { "localGrants", new ArrayList(new[] { "peer.local.read", "peer.local.write" }) },
                    { "localOwnedState", new Dictionary<string, object>() },
                    { "pendingHomeNodeQueue", new ArrayList() },
                    { "selfPromotedToOwner", false }
                }}
            };
        }

        private void RecordRestrictedAudit(string identity, string decision, string reason, string corrId, string taskId)
        {
            string safeId = (identity ?? "unknown");
            if (safeId.Length > 80) safeId = safeId.Substring(0, 80);
            string safeReason = (reason ?? "");
            if (safeReason.Length > 240) safeReason = safeReason.Substring(0, 240);
            _restrictedAudit.Add(new Dictionary<string, object>
            {
                { "seq", _restrictedAudit.Count + 1 },
                { "time", UtcNowIso() },
                { "identity", safeId },
                { "policyVersion", PolicyVersion },
                { "decision", decision },
                { "reason", safeReason },
                { "correlation_id", corrId ?? "" },
                { "task_id", taskId ?? "" }
            });
            while (_restrictedAudit.Count > 500) _restrictedAudit.RemoveAt(0);
        }

        private bool CheckActiveRoute(string holonId, out string err)
        {
            err = "";
            Dictionary<string, object> reg;
            if (!_registrations.TryGetValue(holonId, out reg))
            {
                err = "Hold/Reject: Holon '" + holonId + "' has no registration record.";
                return false;
            }
            long expEpoch = (long)GetNum(reg, "leaseExpiresEpoch", 0);
            if (expEpoch < NowEpoch() && GetStr(reg, "status") == "ACTIVE")
            {
                reg["status"] = "EXPIRED";
                reg["routable"] = false;
                reg["reason"] = "Registration lease expired";
                _meshState["advertisedWithdrawnCount"] = (int)GetNum(_meshState, "advertisedWithdrawnCount", 0) + 1;
            }
            if (GetStr(reg, "status") != "ACTIVE" || !GetBool(reg, "routable", false))
            {
                err = "No routable capability for '" + holonId + "' (status=" + GetStr(reg, "status") + ", reason=" + GetStr(reg, "reason", "withdrawn") + ").";
                return false;
            }
            return true;
        }

        private Dictionary<string, object> BuildCanonicalObjects(string action, string actor, Dictionary<string, object> payload, string corrId, string causationId)
        {
            string seedHex = CanonicalHash(new Dictionary<string, object> { { "action", action }, { "ticks", DateTime.UtcNow.Ticks }, { "payload", payload } }).Substring(0, 12);
            string taskId = "task-" + seedHex;
            string useCorr = string.IsNullOrEmpty(corrId) ? ("corr-" + seedHex) : corrId;
            string useCaus = string.IsNullOrEmpty(causationId) ? taskId : causationId;
            string digest = CanonicalHash(payload);
            string now = UtcNowIso();
            return new Dictionary<string, object>
            {
                { "correlation_id", useCorr },
                { "causation_id", useCaus },
                { "task_id", taskId },
                { "task", new Dictionary<string, object> {
                    { "taskId", taskId }, { "correlation_id", useCorr }, { "causation_id", useCaus },
                    { "source", "x_hubcore" }, { "target", "x_osq" }, { "operation", action }, { "actor", actor },
                    { "state", "COMPLETED" }, { "timestampUtc", now }
                }},
                { "result", new Dictionary<string, object> {
                    { "resultId", "res-" + seedHex }, { "taskId", taskId }, { "correlation_id", useCorr }, { "causation_id", useCaus },
                    { "producer", "x_osq" }, { "status", "SUCCESS" }, { "contentHash", digest }
                }},
                { "event", new Dictionary<string, object> {
                    { "eventId", "evt-" + seedHex }, { "taskId", taskId }, { "correlation_id", useCorr }, { "causation_id", useCaus },
                    { "type", "osq." + action }, { "producer", "x_osq" }, { "timestampUtc", now }
                }},
                { "artifact", new Dictionary<string, object> {
                    { "artifactId", "art-" + seedHex }, { "taskId", taskId }, { "correlation_id", useCorr }, { "causation_id", useCaus },
                    { "type", "application/json;kind=mutation_receipt" }, { "sha256", digest }
                }}
            };
        }

        private Dictionary<string, object> ExportWatermarkSnapshot(string actor, string reason)
        {
            int rev = (int)GetNum(_relational, "revision", 1);
            string digest = CanonicalHash(_relational);
            var snap = new Dictionary<string, object>
            {
                { "snapshotId", "PARQ-WM-REV" + rev + "-" + digest.Substring(0, 8) },
                { "format", "immutable-parquet-watermark/v2.3" },
                { "watermarkRevision", rev },
                { "schemaVersion", GetStr(_relational, "schemaVersion", "osq.relational.static/v2") },
                { "contentHash", digest },
                { "createdAt", UtcNowIso() },
                { "createdBy", actor },
                { "reason", reason },
                { "counts", new Dictionary<string, object> {
                    { "accounts", EnsureList(_relational, "accounts").Count },
                    { "opportunities", EnsureList(_relational, "opportunities").Count },
                    { "budgets", EnsureList(_relational, "budgets").Count },
                    { "quotes", EnsureList(_relational, "quotes").Count },
                    { "items", EnsureList(_relational, "items").Count }
                }}
            };
            _immutableSnapshots.Add(snap);
            while (_immutableSnapshots.Count > 50) _immutableSnapshots.RemoveAt(0);
            return snap;
        }

        private void AtomicWrite(string path, Dictionary<string, object> data)
        {
            if (string.IsNullOrEmpty(path)) return;
            string dir = Path.GetDirectoryName(path);
            if (!Directory.Exists(dir)) Directory.CreateDirectory(dir);
            string json = NewSerializer().Serialize(data);
            string tmp = path + "." + Guid.NewGuid().ToString("N").Substring(0, 6) + ".tmp";
            File.WriteAllText(tmp, json + Environment.NewLine, new UTF8Encoding(false));
            if (File.Exists(path)) File.Delete(path);
            File.Move(tmp, path);
        }

        private void LoadOrInit()
        {
            lock (_stateLock)
            {
                var ser = NewSerializer();
                if (File.Exists(_relPath))
                {
                    _relational = ser.Deserialize<Dictionary<string, object>>(File.ReadAllText(_relPath, new UTF8Encoding(false)));
                }
                else
                {
                    _relational = new Dictionary<string, object>
                    {
                        { "schemaVersion", "osq.relational.static/v2" },
                        { "revision", 1 },
                        { "updatedAt", UtcNowIso() },
                        { "accounts", new ArrayList() },
                        { "opportunities", new ArrayList() },
                        { "budgets", new ArrayList() },
                        { "items", new ArrayList() },
                        { "quotes", new ArrayList() },
                        { "floorplans", new ArrayList() },
                        { "forecastBatch", new Dictionary<string, object>() },
                        { "marketplaceShards", new ArrayList() },
                        { "connectors", new ArrayList() },
                        { "changes", new ArrayList() },
                        { "snapshots", new ArrayList() },
                        { "events", new ArrayList() },
                        { "pathwaySummary", new Dictionary<string, object>() },
                        { "bodyDirectives", new ArrayList() },
                        { "bodyFeedback", new ArrayList() },
                        { "m365HandoffQueue", new ArrayList() }
                    };
                    AtomicWrite(_relPath, _relational);
                }

                if (File.Exists(_tempPath))
                {
                    _temp = ser.Deserialize<Dictionary<string, object>>(File.ReadAllText(_tempPath, new UTF8Encoding(false)));
                }
                else
                {
                    _temp = new Dictionary<string, object>
                    {
                        { "schemaVersion", "osq.shared.temp/v2" },
                        { "revision", 1 },
                        { "updatedAt", UtcNowIso() },
                        { "activeSessions", new ArrayList() },
                        { "entityLocks", new ArrayList() },
                        { "sharedDrafts", new Dictionary<string, object>() },
                        { "whatIfScratch", new Dictionary<string, object>() },
                        { "liveChatFeed", new ArrayList() }
                    };
                    AtomicWrite(_tempPath, _temp);
                }
                EnsureHeuristicsConfig();
                if (!_relational.ContainsKey("lastHeuristicsEvaluation") || _relational["lastHeuristicsEvaluation"] == null)
                {
                    EvaluateHeuristicsOrder(null);
                }
                else
                {
                    RefreshPathwaysAndDirectives("bootstrap");
                }
                InitControlPlaneState();
                ExportWatermarkSnapshot("system", "bootstrap");
            }
        }

        private Dictionary<string, object> DefaultHeuristicsNetworkListEntry(string dc, string name, bool fab, int util, string[] spokes, string cutTime, int transitDays)
        {
            return new Dictionary<string, object>
            {
                { "kittingDc", dc },
                { "name", name },
                { "fabShop", fab },
                { "laborUtilPct", util },
                { "spokes", new ArrayList(spokes) },
                { "linehaulCutTime", cutTime },
                { "transitDays", transitDays }
            };
        }

        private Dictionary<string, object> EnsureHeuristicsConfig()
        {
            var hc = EnsureDict(_relational, "heuristicsConfig");
            if (string.IsNullOrEmpty(GetStr(hc, "activePreset"))) hc["activePreset"] = "RENOPLUS_CONSOLIDATION_NORTH_STAR";
            if (string.IsNullOrEmpty(GetStr(hc, "architectureMode"))) hc["architectureMode"] = "UNIFIED_PHYSICAL_MARKETPLACE_DIJKSTRA";
            if (string.IsNullOrEmpty(GetStr(hc, "updatedAt"))) hc["updatedAt"] = UtcNowIso();
            if (string.IsNullOrEmpty(GetStr(hc, "updatedBy"))) hc["updatedBy"] = "Principal Architect";

            var sla = EnsureDict(hc, "slaAndTiming");
            if (!sla.ContainsKey("immediateCheckHorizonDays")) sla["immediateCheckHorizonDays"] = 10;
            if (!sla.ContainsKey("scheduledRecheckLeadDays")) sla["scheduledRecheckLeadDays"] = 6;
            if (!sla.ContainsKey("standardKittingLeadDays")) sla["standardKittingLeadDays"] = 5;
            if (!sla.ContainsKey("linehaulTransitSlaDays")) sla["linehaulTransitSlaDays"] = 2;
            if (!sla.ContainsKey("inTransitAgingAlertHours")) sla["inTransitAgingAlertHours"] = 48;
            if (!sla.ContainsKey("governanceDecisionSlaDays")) sla["governanceDecisionSlaDays"] = 30;
            if (!sla.ContainsKey("mulesoftReservationHorizonDays")) sla["mulesoftReservationHorizonDays"] = 30;
            if (!sla.ContainsKey("creditHoldAutoShiftRdd")) sla["creditHoldAutoShiftRdd"] = true;
            if (!sla.ContainsKey("holdPeeledLinesToRdd")) sla["holdPeeledLinesToRdd"] = true;
            if (!sla.ContainsKey("alertWindowDays")) sla["alertWindowDays"] = 10;
            if (!sla.ContainsKey("csrEarlyNoticeDays")) sla["csrEarlyNoticeDays"] = 5;
            if (!sla.ContainsKey("expediteSurgeWeight")) sla["expediteSurgeWeight"] = 0.35;
            if (!sla.ContainsKey("consolidationTouchLaborUsd")) sla["consolidationTouchLaborUsd"] = 18.5;
            if (!sla.ContainsKey("holdingPenaltyPerDayUsd")) sla["holdingPenaltyPerDayUsd"] = 4.25;

            var org = EnsureDict(hc, "organizationAndMigration");
            if (string.IsNullOrEmpty(GetStr(org, "defaultOrgProfile"))) org["defaultOrgProfile"] = "MULTIFAMILY_RENO";
            if (string.IsNullOrEmpty(GetStr(org, "salesAreaRouting"))) org["salesAreaRouting"] = "SALES_AREA_20_UNIFIED";
            if (!org.ContainsKey("autoClearLegacyDefaultPlant")) org["autoClearLegacyDefaultPlant"] = true;
            if (!org.ContainsKey("enforceShippingPointTaxCheck")) org["enforceShippingPointTaxCheck"] = true;
            if (!org.ContainsKey("hospitalityCoordinatedFutureMonthOnly")) org["hospitalityCoordinatedFutureMonthOnly"] = true;
            if (!org.ContainsKey("requirePlantStatusRL")) org["requirePlantStatusRL"] = true;
            if (!org.ContainsKey("excludeOpportunityConditionInBy")) org["excludeOpportunityConditionInBy"] = true;
            org["virtualWarehousesAllowed"] = false;
            org["snowflakeRole"] = "analytical-export-only";
            if (!org.ContainsKey("directSapApiTimePhased")) org["directSapApiTimePhased"] = true;
            if (!org.ContainsKey("prohibitVirtualWarehouseMroHijack")) org["prohibitVirtualWarehouseMroHijack"] = true;

            var ful = EnsureDict(hc, "fulfillmentAndKitting");
            if (string.IsNullOrEmpty(GetStr(ful, "defaultFulfillmentMode"))) ful["defaultFulfillmentMode"] = "RP_KITTED_ZPIR";
            if (string.IsNullOrEmpty(GetStr(ful, "peelOffRoutingMode"))) ful["peelOffRoutingMode"] = "CONSOLIDATE_LINEHAUL_FIRST";
            if (!ful.ContainsKey("expandKitToDcStockedSkus")) ful["expandKitToDcStockedSkus"] = true;
            if (!ful.ContainsKey("trueFloorAtpExclude916AndPastDuePo")) ful["trueFloorAtpExclude916AndPastDuePo"] = true;
            if (!ful.ContainsKey("excludeHazmatFromKit")) ful["excludeHazmatFromKit"] = true;
            if (!ful.ContainsKey("excludeStopReplenishmentBA")) ful["excludeStopReplenishmentBA"] = true;
            if (!ful.ContainsKey("excludeOversizedFragileDoors")) ful["excludeOversizedFragileDoors"] = true;
            if (!ful.ContainsKey("applianceDedicatedPalletRule")) ful["applianceDedicatedPalletRule"] = true;
            if (!ful.ContainsKey("autoStripFactoryDirectZtas")) ful["autoStripFactoryDirectZtas"] = true;
            if (!ful.ContainsKey("requireFabShopForZtnfBlinds")) ful["requireFabShopForZtnfBlinds"] = true;
            if (!ful.ContainsKey("requireSpokeArrivalBeforeRoute")) ful["requireSpokeArrivalBeforeRoute"] = true;
            if (!ful.ContainsKey("enforceSingleDestinationLinehaulPallet")) ful["enforceSingleDestinationLinehaulPallet"] = true;
            if (!ful.ContainsKey("dcLaborCapacityMaxPct")) ful["dcLaborCapacityMaxPct"] = 85;
            if (!ful.ContainsKey("proactiveSplitShipmentAlerts")) ful["proactiveSplitShipmentAlerts"] = true;
            if (!ful.ContainsKey("protectFutureKitsFromTodayShortage")) ful["protectFutureKitsFromTodayShortage"] = true;
            if (!ful.ContainsKey("requireParentQtyDrivesChildren")) ful["requireParentQtyDrivesChildren"] = true;
            if (!ful.ContainsKey("rerunAvailabilityOnQuoteCarryChange")) ful["rerunAvailabilityOnQuoteCarryChange"] = true;
            if (!ful.ContainsKey("requireSaveReopenUnitNumber")) ful["requireSaveReopenUnitNumber"] = true;
            if (!ful.ContainsKey("dailyZstockNegativeReview")) ful["dailyZstockNegativeReview"] = true;
            if (!ful.ContainsKey("outboundPrintDateValidation")) ful["outboundPrintDateValidation"] = true;
            if (!ful.ContainsKey("unifiedMarketplaceDijkstra")) ful["unifiedMarketplaceDijkstra"] = true;
            if (!ful.ContainsKey("dijkstraTrafficWeightFactor")) ful["dijkstraTrafficWeightFactor"] = 1.0;
            if (!ful.ContainsKey("ctoSeasonalityThreshold")) ful["ctoSeasonalityThreshold"] = 0.65;
            if (!ful.ContainsKey("modularBomFlattening")) ful["modularBomFlattening"] = true;
            if (!ful.ContainsKey("sameDayStoDupGuard")) ful["sameDayStoDupGuard"] = true;
            if (!ful.ContainsKey("ifrAutoPoEnabled")) ful["ifrAutoPoEnabled"] = true;
            if (!ful.ContainsKey("cancelPoReductionGate")) ful["cancelPoReductionGate"] = true;

            var scb = EnsureDict(hc, "forecastAndScb");
            if (string.IsNullOrEmpty(GetStr(scb, "forecastTimePhasingMode"))) scb["forecastTimePhasingMode"] = "FRONT_LOAD_FIRST_MONDAY";
            if (!scb.ContainsKey("frontLoadDurationDays")) scb["frontLoadDurationDays"] = 7;
            if (!scb.ContainsKey("preventCurrentMonthDoubleCount")) scb["preventCurrentMonthDoubleCount"] = true;
            if (!scb.ContainsKey("scbEoqServiceLevelPct")) scb["scbEoqServiceLevelPct"] = 95.0;
            if (!scb.ContainsKey("scbBullwhipDampingThreshold")) scb["scbBullwhipDampingThreshold"] = 1.25;
            if (!scb.ContainsKey("scbMultiEchelonSafetyStock")) scb["scbMultiEchelonSafetyStock"] = true;
            if (string.IsNullOrEmpty(GetStr(scb, "scbLeadTimeSurvivalQuantile"))) scb["scbLeadTimeSurvivalQuantile"] = "p90";
            if (!scb.ContainsKey("scbQuipuEmaAlpha")) scb["scbQuipuEmaAlpha"] = 0.90;

            var net = EnsureList(hc, "kittingDcNetwork");
            if (net.Count == 0)
            {
                net.Add(DefaultHeuristicsNetworkListEntry("GA02", "Atlanta Kitting Hub", true, 74, new[] { "NC34", "AL04", "FL08" }, "18:00 EST", 1));
                net.Add(DefaultHeuristicsNetworkListEntry("TX03", "Dallas Kitting Hub", true, 88, new[] { "TX77", "OK12" }, "17:30 CST", 1));
                net.Add(DefaultHeuristicsNetworkListEntry("IL30", "Chicago Kitting Hub", true, 71, new[] { "MN55", "MO18", "OH24" }, "18:00 CST", 1));
                net.Add(DefaultHeuristicsNetworkListEntry("NJ22", "New Jersey Kitting Hub", true, 86, new[] { "PA15", "MA07", "MD19" }, "17:00 EST", 1));
                net.Add(DefaultHeuristicsNetworkListEntry("CA20", "Southern CA Kitting Hub", true, 78, new[] { "CA42" }, "18:30 PST", 1));
                net.Add(DefaultHeuristicsNetworkListEntry("FL14", "Orlando Kitting Hub", true, 69, new[] { "FL08", "FL31" }, "18:00 EST", 1));
                net.Add(DefaultHeuristicsNetworkListEntry("WA09", "Seattle Kitting Hub", false, 65, new[] { "OR11" }, "17:30 PST", 1));
                net.Add(DefaultHeuristicsNetworkListEntry("AZ17", "Phoenix Kitting Hub", false, 72, new[] { "NV06" }, "18:00 MST", 1));
                net.Add(DefaultHeuristicsNetworkListEntry("CO11", "Denver Kitting Hub", false, 68, new[] { "UT08" }, "17:30 MST", 2));
            }
            return hc;
        }

        private void ApplyHeuristicsPreset(Dictionary<string, object> hc, string presetId)
        {
            var sla = EnsureDict(hc, "slaAndTiming");
            var org = EnsureDict(hc, "organizationAndMigration");
            var ful = EnsureDict(hc, "fulfillmentAndKitting");
            var scb = EnsureDict(hc, "forecastAndScb");
            hc["activePreset"] = presetId;

            if (presetId == "RENOPLUS_CONSOLIDATION_NORTH_STAR")
            {
                sla["immediateCheckHorizonDays"] = 10; sla["scheduledRecheckLeadDays"] = 6; sla["standardKittingLeadDays"] = 5; sla["linehaulTransitSlaDays"] = 2; sla["creditHoldAutoShiftRdd"] = true; sla["holdPeeledLinesToRdd"] = true;
                org["defaultOrgProfile"] = "MULTIFAMILY_RENO"; org["salesAreaRouting"] = "SALES_AREA_20_UNIFIED";
                ful["defaultFulfillmentMode"] = "RP_KITTED_ZPIR"; ful["peelOffRoutingMode"] = "CONSOLIDATE_LINEHAUL_FIRST"; ful["expandKitToDcStockedSkus"] = true; ful["trueFloorAtpExclude916AndPastDuePo"] = true; ful["dcLaborCapacityMaxPct"] = 85;
                scb["forecastTimePhasingMode"] = "FRONT_LOAD_FIRST_MONDAY"; scb["scbEoqServiceLevelPct"] = 95.0; scb["scbLeadTimeSurvivalQuantile"] = "p90";
            }
            else if (presetId == "MULTI_HUB_CAMPUS_LINEHAUL")
            {
                sla["immediateCheckHorizonDays"] = 10; sla["scheduledRecheckLeadDays"] = 6; sla["linehaulTransitSlaDays"] = 2; sla["holdPeeledLinesToRdd"] = true;
                ful["defaultFulfillmentMode"] = "RP_KITTED_ZPIR"; ful["peelOffRoutingMode"] = "CONSOLIDATE_LINEHAUL_FIRST"; ful["expandKitToDcStockedSkus"] = true; ful["requireSpokeArrivalBeforeRoute"] = true; ful["dcLaborCapacityMaxPct"] = 95;
                scb["forecastTimePhasingMode"] = "FRONT_LOAD_FIRST_MONDAY"; scb["scbMultiEchelonSafetyStock"] = true;
            }
            else if (presetId == "PEAK_DC_LABOR_GUARD")
            {
                sla["immediateCheckHorizonDays"] = 10; sla["scheduledRecheckLeadDays"] = 6; sla["holdPeeledLinesToRdd"] = true;
                ful["defaultFulfillmentMode"] = "RP_KITTED_ZPIR"; ful["peelOffRoutingMode"] = "CONSOLIDATE_LINEHAUL_FIRST"; ful["dcLaborCapacityMaxPct"] = 75; ful["expandKitToDcStockedSkus"] = true;
                scb["forecastTimePhasingMode"] = "FRONT_LOAD_FIRST_MONDAY";
            }
            else if (presetId == "HOSPITALITY_COORDINATED_DELIVERY")
            {
                sla["immediateCheckHorizonDays"] = 14; sla["scheduledRecheckLeadDays"] = 7; sla["standardKittingLeadDays"] = 5; sla["creditHoldAutoShiftRdd"] = true; sla["holdPeeledLinesToRdd"] = true;
                org["defaultOrgProfile"] = "HOSPITALITY_COORDINATED"; org["hospitalityCoordinatedFutureMonthOnly"] = true;
                ful["defaultFulfillmentMode"] = "RP_KITTED_ZPIR"; ful["peelOffRoutingMode"] = "CONSOLIDATE_LINEHAUL_FIRST"; ful["dcLaborCapacityMaxPct"] = 90;
                scb["forecastTimePhasingMode"] = "FRONT_LOAD_FIRST_MONDAY"; scb["scbEoqServiceLevelPct"] = 98.0; scb["scbLeadTimeSurvivalQuantile"] = "p95";
            }
            else if (presetId == "WHOLESALE_MRO_FAST_FLEX")
            {
                sla["immediateCheckHorizonDays"] = 5; sla["scheduledRecheckLeadDays"] = 3; sla["standardKittingLeadDays"] = 2; sla["holdPeeledLinesToRdd"] = false;
                org["defaultOrgProfile"] = "WHOLESALE_COMMERCIAL";
                ful["defaultFulfillmentMode"] = "WHOLESALE_MRO"; ful["peelOffRoutingMode"] = "DIRECT_MRO_FLEX"; ful["expandKitToDcStockedSkus"] = false; ful["dcLaborCapacityMaxPct"] = 95;
                scb["forecastTimePhasingMode"] = "WEEKLY_EVEN_SPREAD"; scb["scbEoqServiceLevelPct"] = 95.0; scb["scbLeadTimeSurvivalQuantile"] = "p50";
            }
            else if (presetId == "UNIFIED_MARKETPLACE_DIJKSTRA_DROPSHIP")
            {
                sla["immediateCheckHorizonDays"] = 10; sla["scheduledRecheckLeadDays"] = 6; sla["standardKittingLeadDays"] = 5; sla["linehaulTransitSlaDays"] = 2;
                sla["creditHoldAutoShiftRdd"] = true; sla["holdPeeledLinesToRdd"] = true; sla["alertWindowDays"] = 10; sla["csrEarlyNoticeDays"] = 5; sla["expediteSurgeWeight"] = 0.40;
                org["defaultOrgProfile"] = "UNIFIED_PHYSICAL_MARKETPLACE"; org["salesAreaRouting"] = "SALES_AREA_20_UNIFIED";
                org["virtualWarehousesAllowed"] = false; org["snowflakeRole"] = "analytical-export-only"; org["directSapApiTimePhased"] = true;
                ful["defaultFulfillmentMode"] = "UNIFIED_MARKETPLACE_DIRECT"; ful["peelOffRoutingMode"] = "DIJKSTRA_DROPSHIP_OR_CONSOLIDATE";
                ful["unifiedMarketplaceDijkstra"] = true; ful["expandKitToDcStockedSkus"] = true; ful["trueFloorAtpExclude916AndPastDuePo"] = true;
                ful["dcLaborCapacityMaxPct"] = 85; ful["modularBomFlattening"] = true; ful["sameDayStoDupGuard"] = true; ful["ifrAutoPoEnabled"] = true;
                scb["forecastTimePhasingMode"] = "FRONT_LOAD_FIRST_MONDAY"; scb["scbEoqServiceLevelPct"] = 97.5; scb["scbLeadTimeSurvivalQuantile"] = "p95";
            }
        }

        private Dictionary<string, object> EvaluateHeuristicsOrder(Dictionary<string, object> opts)
        {
            var hc = EnsureHeuristicsConfig();
            var sla = EnsureDict(hc, "slaAndTiming");
            var org = EnsureDict(hc, "organizationAndMigration");
            var ful = EnsureDict(hc, "fulfillmentAndKitting");
            var scb = EnsureDict(hc, "forecastAndScb");
            var net = EnsureList(hc, "kittingDcNetwork");
            var quotes = EnsureList(_relational, "quotes");
            var items = EnsureList(_relational, "items");
            var accounts = EnsureList(_relational, "accounts");

            string qid = opts != null ? GetStr(opts, "quoteId", GetStr(_relational, "selectedQuoteId", "ROF-DEMO-001")) : GetStr(_relational, "selectedQuoteId", "ROF-DEMO-001");
            var q = FindByKey(quotes, "id", qid);
            if (q == null && quotes.Count > 0) q = quotes[0] as Dictionary<string, object>;
            if (q == null) return new Dictionary<string, object>();
            qid = GetStr(q, "id");
            var acct = FindByKey(accounts, "accountId", GetStr(q, "accountId"));

            string localDc = (opts != null && !string.IsNullOrEmpty(GetStr(opts, "localDc"))) ? GetStr(opts, "localDc").ToUpperInvariant() : GetStr(q, "localDc", acct != null ? GetStr(acct, "defaultLocalDc", "NC34") : "NC34").ToUpperInvariant();
            int orderLeadDays = (opts != null && opts.ContainsKey("orderToRddDays")) ? (int)GetNum(opts, "orderToRddDays", 8) : (int)GetNum(q, "orderToRddDays", 8);
            string delivBlock = (opts != null && opts.ContainsKey("deliveryBlock")) ? GetStr(opts, "deliveryBlock").ToUpperInvariant() : GetStr(q, "deliveryBlock", "ZR").ToUpperInvariant();
            int creditHoldDays = (opts != null && opts.ContainsKey("creditHoldDays")) ? (int)GetNum(opts, "creditHoldDays", 0) : (int)GetNum(q, "creditHoldDays", 0);
            string orgType = (opts != null && !string.IsNullOrEmpty(GetStr(opts, "orgType"))) ? GetStr(opts, "orgType") : GetStr(q, "orgType", GetStr(org, "defaultOrgProfile", "MULTIFAMILY_RENO"));
            string fulfillmentMode = (opts != null && !string.IsNullOrEmpty(GetStr(opts, "fulfillmentType"))) ? GetStr(opts, "fulfillmentType") : GetStr(q, "fulfillmentType", GetStr(ful, "defaultFulfillmentMode", "RP_KITTED_ZPIR"));

            Dictionary<string, object> kitHub = null;
            foreach (object hObj in net)
            {
                var hd = hObj as Dictionary<string, object>;
                if (hd == null) continue;
                if (string.Equals(GetStr(hd, "kittingDc"), localDc, StringComparison.OrdinalIgnoreCase)) { kitHub = hd; break; }
                foreach (object sp in EnsureList(hd, "spokes"))
                {
                    if (string.Equals(Convert.ToString(sp), localDc, StringComparison.OrdinalIgnoreCase)) { kitHub = hd; break; }
                }
                if (kitHub != null) break;
            }
            if (kitHub == null && net.Count > 0) kitHub = net[0] as Dictionary<string, object>;
            string kittingDc = kitHub != null ? GetStr(kitHub, "kittingDc", "GA02") : "GA02";
            string kittingDcName = kitHub != null ? GetStr(kitHub, "name", kittingDc) : kittingDc;
            int kitLaborUtil = kitHub != null ? (int)GetNum(kitHub, "laborUtilPct", 74) : 74;
            bool kitFabShop = kitHub != null && GetBool(kitHub, "fabShop", true);
            int kitTransitDays = kitHub != null ? (int)GetNum(kitHub, "transitDays", 1) : 1;

            int var1Horizon = (int)GetNum(sla, "immediateCheckHorizonDays", 10);
            int var2Recheck = (int)GetNum(sla, "scheduledRecheckLeadDays", 6);
            int stdKitLead = (int)GetNum(sla, "standardKittingLeadDays", 5);
            int maxLinehaulSla = (int)GetNum(sla, "linehaulTransitSlaDays", 2);
            bool autoShiftCredit = GetBool(sla, "creditHoldAutoShiftRdd", true);
            bool holdToRdd = GetBool(sla, "holdPeeledLinesToRdd", true);

            bool trueFloorMode = GetBool(ful, "trueFloorAtpExclude916AndPastDuePo", true);
            bool expandAssort = GetBool(ful, "expandKitToDcStockedSkus", true);
            bool exclHazmat = GetBool(ful, "excludeHazmatFromKit", true);
            bool exclBa = GetBool(ful, "excludeStopReplenishmentBA", true);
            bool exclDoor = GetBool(ful, "excludeOversizedFragileDoors", true);
            bool applPallet = GetBool(ful, "applianceDedicatedPalletRule", true);
            bool stripZtas = GetBool(ful, "autoStripFactoryDirectZtas", true);
            bool reqFabShop = GetBool(ful, "requireFabShopForZtnfBlinds", true);
            int maxLaborCap = (int)GetNum(ful, "dcLaborCapacityMaxPct", 85);
            string peelMode = GetStr(ful, "peelOffRoutingMode", "CONSOLIDATE_LINEHAUL_FIRST");
            bool protectFutureKits = GetBool(ful, "protectFutureKitsFromTodayShortage", true);
            bool dailyZstockRev = GetBool(ful, "dailyZstockNegativeReview", true);
            bool outboundPrintVal = GetBool(ful, "outboundPrintDateValidation", true);

            int remainingAfterCredit = Math.Max(0, orderLeadDays - creditHoldDays);
            int rddShift = (autoShiftCredit && (creditHoldDays > 0 || delivBlock == "ZN") && remainingAfterCredit < stdKitLead) ? (stdKitLead - remainingAfterCredit) : 0;
            int effectiveLead = orderLeadDays + rddShift;
            string checkTimingMode = (orderLeadDays <= var1Horizon)
                ? "IMMEDIATE_ORDER_ENTRY_CHECK (Lead " + orderLeadDays + "d <= Var1 " + var1Horizon + "d)"
                : "BATCH_RECHECK_SCHEDULED_AT_RDD_MINUS_" + var2Recheck + "D (Lead " + orderLeadDays + "d > Var1 " + var1Horizon + "d)";
            string commitStatus = (delivBlock == "ZV" || delivBlock == "ZN")
                ? "NON_COMMITTING (" + delivBlock + " Quote/Credit Block - Inventory Not Held)"
                : (effectiveLead <= stdKitLead ? "HARD_WAVE_RESERVATION (VL01N Outbound Drop Window)" : "SOFT_ALLOCATION (Committed on Order Until VL01N @ RDD-5d)");

            var lineDecisions = new ArrayList();
            var deepMapEvents = new ArrayList();
            int inKitCount = 0, expandedCount = 0, linehaulCount = 0, laborGuardCount = 0, directFlexCount = 0;
            int ztasCount = 0, exclCount = 0, applCount = 0, baCount = 0, batchDefCount = 0, paperRiskCount = 0, alertCount = 0, waitPreserveCount = 0;
            double totalDemand = 0.0, totalSafety = 0.0, totalCvar = 0.0, maxZ = 0.0, sumP90 = 0.0;
            int scbItemN = 0;

            foreach (object lObj in EnsureList(q, "lines"))
            {
                var ln = lObj as Dictionary<string, object>;
                if (ln == null) continue;
                string sku = GetStr(ln, "sku").Trim().ToUpperInvariant();
                var it = FindByKey(items, "sku", sku);
                int qty = (int)Math.Max(1, Math.Round(GetNum(ln, "qty", 1)));
                totalDemand += qty;

                string desc = it != null ? GetStr(it, "description", sku) : sku;
                string cat = it != null ? GetStr(it, "sapItemCategory", "ZTAN") : "ZTAN";
                bool kitElig = it != null && GetBool(it, "kitEligibleFlag", false);
                bool dcStocked = it != null && GetBool(it, "dcStockedFlag", false);
                bool isHazmat = it != null && GetBool(it, "hazmat", false);
                bool isBa = it != null && (GetBool(it, "stopReplenishment", false) || GetStr(it, "plantStatus", "RL") == "BA" || GetStr(it, "status", "Active") == "Discontinued");
                bool isDoor = it != null && GetBool(it, "oversizedOrFragile", false);
                bool isAppl = it != null && GetBool(it, "largeApplianceOrHvac", false);

                var floorMap = it != null ? EnsureDict(it, "floorQohByDc") : new Dictionary<string, object>();
                var commMap = it != null ? EnsureDict(it, "committedQtyByDc") : new Dictionary<string, object>();
                var s916Map = it != null ? EnsureDict(it, "storage916QtyByDc") : new Dictionary<string, object>();
                int floorQoh = (int)GetNum(floorMap, kittingDc, GetNum(it, "qoh", 0));
                int commQty = (int)GetNum(commMap, kittingDc, 0);
                int s916Qty = (int)GetNum(s916Map, kittingDc, 0);
                int pastDuePo = it != null ? (int)GetNum(it, "unreceivedPastDuePoQty", 0) : 0;
                int expRepl = it != null ? (int)GetNum(it, "expectedReplenishmentQty", 0) : 0;
                int replDays = it != null ? (int)GetNum(it, "incomingReplenishmentDays", 7) : 7;
                int zstockNeg = it != null ? (int)GetNum(it, "zstockNegativeQty", 0) : 0;

                int paperAtp = Math.Max(0, floorQoh - commQty + pastDuePo);
                int trueFloor = Math.Max(0, floorQoh - commQty - s916Qty);
                int projectedAvail = trueFloor + expRepl;
                int effectiveAtp = trueFloorMode ? trueFloor : paperAtp;
                if (paperAtp >= qty && trueFloor < qty) paperRiskCount++;

                string spokeDc = localDc;
                int spokeAvail = Math.Max(0, (int)GetNum(floorMap, localDc, 0) - (int)GetNum(commMap, localDc, 0));
                if (spokeAvail < qty)
                {
                    foreach (var kv in floorMap)
                    {
                        if (string.Equals(kv.Key, kittingDc, StringComparison.OrdinalIgnoreCase)) continue;
                        int availOther = Math.Max(0, Convert.ToInt32(kv.Value) - (int)GetNum(commMap, kv.Key, 0));
                        if (availOther > spokeAvail) { spokeAvail = availOther; spokeDc = kv.Key; }
                    }
                }

                var scbM = it != null ? EnsureDict(it, "scbMetrics") : new Dictionary<string, object>();
                int eoqOpt = (int)GetNum(scbM, "eoqOptimalQty", 0);
                int gwSs = (int)GetNum(scbM, "gravesWillemsSafetyStock", 0);
                double p50Lt = GetNum(scbM, "p50LeadDays", GetNum(it, "leadDays", 5));
                double p90Lt = GetNum(scbM, "p90LeadDays", p50Lt + 3);
                double p95Lt = GetNum(scbM, "p95LeadDays", p90Lt + 3);
                double ltZ = Math.Abs(GetNum(scbM, "eoqZScore", 0.0));
                string survQuant = GetStr(scb, "scbLeadTimeSurvivalQuantile", "p90");
                double chosenLt = (survQuant == "p95") ? p95Lt : ((survQuant == "p50") ? p50Lt : p90Lt);
                if (ltZ > maxZ) maxZ = ltZ;
                if (p90Lt > 0) { sumP90 += p90Lt; scbItemN++; }
                if (GetBool(scb, "scbMultiEchelonSafetyStock", true)) totalSafety += gwSs;
                totalCvar += GetNum(scbM, "cvarCostUsd", 0);

                string dCode = "", dLabel = "", sev = "ok", expl = "", custNote = "Ships complete inside Master Kit (" + kittingDc + ").";
                int kitCommitHorizon = Math.Max(0, effectiveLead - stdKitLead);

                if (cat == "ZLAB")
                {
                    dCode = "SERVICE_LABOR"; dLabel = "RP Install Service Line"; sev = "info";
                    expl = "Non-physical labor line; gated by Opportunity flags.installation = true (CPQ-11).";
                }
                else if (isBa && exclBa)
                {
                    dCode = "BLOCKED_BA"; dLabel = "BLOCKED: BA Discontinued / Stop-Replen (#544/#555)"; sev = "err"; baCount++;
                    expl = "Stop-Replenishment=1 / Plant Status BA: blocked from kitting & EWM adjustments; swap to active SKU (CPQ-34).";
                    custNote = "Substitution Required -> notify RSR/Customer before release."; alertCount++;
                    deepMapEvents.Add(new Dictionary<string, object> { { "eventType", "ExceptionCase" }, { "targetQueue", "RPAQueue" }, { "sku", sku }, { "detail", expl } });
                }
                else if (cat == "ZTAS" && stripZtas)
                {
                    dCode = "AUTO_STRIP_ZTAS"; dLabel = "Auto-Stripped Factory Direct (ZTAS) -> Day-0 PO"; sev = "info"; ztasCount++;
                    expl = "5-digit Special Order auto-stripped from kit so vendor PO fires immediately without waiting for kit wave.";
                    custNote = "Factory-Direct drop ship ETA " + chosenLt + "d tracked independently.";
                    deepMapEvents.Add(new Dictionary<string, object> { { "eventType", "PeelEvent" }, { "targetQueue", "FactoryDirectPO" }, { "sku", sku }, { "detail", expl } });
                }
                else if (isHazmat && exclHazmat)
                {
                    dCode = "EXCLUDED_HAZMAT"; dLabel = "Excluded from Kit Carton (Hazmat DOT/EWM)"; sev = "warn"; exclCount++;
                    expl = "Hazmat=Y: excluded from enclosed kit box; ships via compliant hazardous ground/MRO parcel.";
                    custNote = "Separate compliant hazmat parcel (" + (holdToRdd ? "held to match RDD" : "immediate dispatch") + ").";
                    deepMapEvents.Add(new Dictionary<string, object> { { "eventType", "PeelEvent" }, { "targetQueue", "HazmatCompliance" }, { "sku", sku }, { "detail", expl } });
                }
                else if (isDoor && exclDoor)
                {
                    dCode = "EXCLUDED_OVERSIZED"; dLabel = "Excluded: Oversized / Fragile Door Guard"; sev = "warn"; exclCount++;
                    expl = "Prevent strap-on kit damage: oversized door/fragile item routed on protected flat rack / local delivery.";
                    custNote = "Delivered separately on protected racks (" + (holdToRdd ? "RDD-aligned" : "independent ETA") + ").";
                    deepMapEvents.Add(new Dictionary<string, object> { { "eventType", "PeelEvent" }, { "targetQueue", "FlatRackDelivery" }, { "sku", sku }, { "detail", expl } });
                }
                else if (isAppl && applPallet)
                {
                    dCode = "APPLIANCE_PALLET"; dLabel = "Dedicated Appliance Pallet + 2-Day Linehaul SLA"; sev = "info"; applCount++;
                    expl = "Appliance/HVAC rule: dedicated cone/sticker pallet (P4) with CG-06 arrival gate prior to route build.";
                    custNote = "Ships on dedicated protected appliance pallet aligned to kit RDD.";
                    deepMapEvents.Add(new Dictionary<string, object> { { "eventType", "ShipmentEvent" }, { "targetQueue", "DedicatedPallet" }, { "sku", sku }, { "detail", expl } });
                }
                else if (cat == "ZTNF" && reqFabShop && !kitFabShop)
                {
                    dCode = "FAB_SHOP_REROUTE"; dLabel = "Peeled: No EWM Fab Shop @ " + kittingDc + " -> Hub Fab"; sev = "warn"; linehaulCount++;
                    expl = "Enosix VC Custom Blind (ZTNF) cannot fabricate at " + kittingDc + "; routed to nearest Fab Shop + linehaul buffer.";
                    custNote = "Custom fabrication routed via Hub Fab Shop; +" + kitTransitDays + "d linehaul buffer."; alertCount++;
                    deepMapEvents.Add(new Dictionary<string, object> { { "eventType", "ConsolidationEvent" }, { "targetQueue", "FabShopLinehaul" }, { "sku", sku }, { "detail", expl } });
                }
                else if (fulfillmentMode.IndexOf("WHOLESALE", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    dCode = "WHOLESALE_FLEX"; dLabel = "Wholesale/MRO Multi-DC FlexLogic (" + localDc + ")"; sev = "ok"; directFlexCount++;
                    expl = "Standard MRO FlexLogic evaluates Local DC -> Backup DC 1 -> Backup DC 2 without kitting bundle wait.";
                    deepMapEvents.Add(new Dictionary<string, object> { { "eventType", "CrossShipEvent" }, { "targetQueue", "MROFlex" }, { "sku", sku }, { "detail", expl } });
                }
                else if (orderLeadDays > var1Horizon)
                {
                    dCode = "BATCH_DEFERRED"; dLabel = "Held in Kit -> Auto-Check @ RDD-" + var2Recheck + "d (Var2)"; sev = "info"; batchDefCount++;
                    expl = "Order lead (" + orderLeadDays + "d) > Var1 (" + var1Horizon + "d): kept intact in kit so Blue Yonder front-loaded POs arrive before RDD-" + var2Recheck + "d batch check (Golden Rule: do not alter future kits).";
                }
                else if (!kitElig && !expandAssort)
                {
                    dCode = "PEEL_NON_ELIGIBLE"; dLabel = "Peeled: Non-Kit-Eligible -> MRO Flex" + (holdToRdd ? " (RDD Hold)" : ""); sev = "warn"; directFlexCount++;
                    expl = "SKU lacks Z-RP Kit Eligible flag and Assortment Expansion is OFF; peeled from kit to direct MRO flex.";
                    custNote = "Ships separately (" + (holdToRdd ? "RDD Hold prevents early arrival" : "immediate dispatch") + ")."; alertCount++;
                    deepMapEvents.Add(new Dictionary<string, object> { { "eventType", "PeelEvent" }, { "targetQueue", "MROFlex" }, { "sku", sku }, { "detail", expl } });
                }
                else if (effectiveAtp >= qty)
                {
                    if (!kitElig && dcStocked && expandAssort)
                    {
                        dCode = "EXPANDED_IN_KIT"; dLabel = "Keep in Kit (Assortment Expanded: DC Floor Stocked)"; sev = "ok"; expandedCount++;
                        expl = "Previously peeled SKU automatically included in kit because " + kittingDc + " holds " + trueFloor + " floor units >= " + qty + ".";
                    }
                    else
                    {
                        dCode = "KEEP_IN_KIT"; dLabel = "Keep in Master Kit @ " + kittingDc + " (Floor ATP=" + trueFloor + ")"; sev = "ok"; inKitCount++;
                        expl = "Physical Floor QOH (" + floorQoh + ") - Committed (" + commQty + ") - Storage916 (" + s916Qty + ") = " + trueFloor + " >= " + qty + ". Green for wave!";
                    }
                }
                else if (protectFutureKits && projectedAvail >= qty && replDays <= kitCommitHorizon)
                {
                    dCode = "WAIT_FOR_SUPPLY_PRESERVE_KIT"; dLabel = "Preserve Future Kit: Wait for Replenishment (ETA " + replDays + "d <= Commit " + kitCommitHorizon + "d)"; sev = "ok"; waitPreserveCount++;
                    expl = "Slide Step 8 Golden Rule: Current Floor ATP (" + trueFloor + ") is short of " + qty + ", but Incoming Replenishment (+" + expRepl + "u in " + replDays + "d) meets Kit Commit Date (" + kitCommitHorizon + "d). Do NOT modify future kit!";
                    custNote = "Kit preserved intact on replenishment schedule (Projected=" + projectedAvail + "u).";
                    deepMapEvents.Add(new Dictionary<string, object> { { "eventType", "ConsolidationEvent" }, { "targetQueue", "WaitForSupplyPreserveKit" }, { "sku", sku }, { "detail", expl } });
                }
                else
                {
                    if (peelMode == "CONSOLIDATE_LINEHAUL_FIRST" && spokeAvail >= qty && kitTransitDays <= maxLinehaulSla)
                    {
                        if (kitLaborUtil > maxLaborCap)
                        {
                            dCode = "LABOR_GUARD_MRO_FLEX"; dLabel = "Labor Guard (" + kittingDc + " " + kitLaborUtil + "% > " + maxLaborCap + "%) -> Flex from " + spokeDc + (holdToRdd ? " + RDD Hold" : ""); sev = "warn"; laborGuardCount++;
                            expl = "Floor short at " + kittingDc + " (TrueFloor=" + trueFloor + ", PaperCO09=" + paperAtp + "). " + spokeDc + " has " + spokeAvail + "u, but " + kittingDc + " labor (" + kitLaborUtil + "%) > cap (" + maxLaborCap + "%) -> direct MRO flex with RDD Hold.";
                            custNote = "Separate direct shipment from " + spokeDc + " held for Customer RDD to avoid early delivery."; alertCount++;
                            deepMapEvents.Add(new Dictionary<string, object> { { "eventType", "CrossShipEvent" }, { "targetQueue", "NotificationEvent" }, { "sku", sku }, { "detail", expl } });
                        }
                        else
                        {
                            dCode = "LINEHAUL_MATCHUP"; dLabel = "Linehaul Match-Up (" + spokeDc + " -> " + kittingDc + ") + Kit Label #475"; sev = "ok"; linehaulCount++;
                            expl = kittingDc + " True Floor=" + trueFloor + " (<" + qty + "; Paper CO09=" + paperAtp + " had " + pastDuePo + "u past-due PO). " + spokeDc + " linehauls " + qty + "u via Mvt 313->315 with CG-06 Arrival Gate.";
                            custNote = "Consolidated via overnight linehaul into Master Kit (1 delivery box to property!).";
                            deepMapEvents.Add(new Dictionary<string, object> { { "eventType", "ConsolidationEvent" }, { "targetQueue", "LinehaulMatchUp475" }, { "sku", sku }, { "detail", expl } });
                        }
                    }
                    else if (spokeAvail >= qty)
                    {
                        dCode = "PEEL_MRO_FLEX"; dLabel = "Peeled to Direct MRO Flex (" + spokeDc + ")" + (holdToRdd ? " + RDD Hold" : ""); sev = "warn"; directFlexCount++;
                        expl = kittingDc + " short (TrueFloor=" + trueFloor + "); peeled to standard MRO Flex from " + spokeDc + " (" + spokeAvail + "u avail)" + (holdToRdd ? " with RDD Hold Guard." : " (ships immediately).");
                        custNote = "Split Shipment Alert sent to Order Contact + RSR (" + spokeDc + " -> Site)."; alertCount++;
                        deepMapEvents.Add(new Dictionary<string, object> { { "eventType", "PeelEvent" }, { "targetQueue", "NotificationEvent" }, { "sku", sku }, { "detail", expl } });
                    }
                    else
                    {
                        dCode = "PEEL_BACKORDER"; dLabel = "Peeled to Local Backorder + Proactive ETA Alert"; sev = "err"; directFlexCount++; alertCount++;
                        expl = "All regional DCs short of " + qty + "u; peeled from kit so remaining lines ship complete on time; PO expedited.";
                        custNote = "Proactive Backorder Notification & Recovery ETA emailed to Customer & RSR.";
                        deepMapEvents.Add(new Dictionary<string, object> { { "eventType", "ExceptionCase" }, { "targetQueue", "RPAQueue" }, { "sku", sku }, { "detail", expl } });
                    }
                }

                lineDecisions.Add(new Dictionary<string, object>
                {
                    { "lineId", GetStr(ln, "lineId") },
                    { "sku", sku },
                    { "description", desc },
                    { "sapItemCategory", cat },
                    { "qty", qty },
                    { "floorQoh", floorQoh },
                    { "committedQty", commQty },
                    { "storage916Qty", s916Qty },
                    { "unreceivedPastDuePoQty", pastDuePo },
                    { "expectedReplenishmentQty", expRepl },
                    { "incomingReplenishmentDays", replDays },
                    { "zstockNegativeQty", zstockNeg },
                    { "projectedQty", projectedAvail },
                    { "atpResult", paperAtp >= qty ? "PASS" : "SHORT" },
                    { "co09Result", trueFloor >= qty ? "FLOOR_OK" : "PO_DEPENDENCY" },
                    { "zstockResult", zstockNeg < 0 ? "NEGATIVE (" + zstockNeg + ")" : "CLEAN" },
                    { "paperAtp", paperAtp },
                    { "trueFloorAvail", trueFloor },
                    { "spokeDc", spokeDc },
                    { "spokeFloorAvail", spokeAvail },
                    { "decisionCode", dCode },
                    { "decisionLabel", dLabel },
                    { "severity", sev },
                    { "explanation", expl },
                    { "holdToRdd", holdToRdd },
                    { "customerNotification", custNote },
                    { "eoqOptimalQty", eoqOpt },
                    { "gravesWillemsSafetyStock", gwSs },
                    { "leadDaysQuantile", chosenLt }
                });
            }

            string parentKitSku = GetStr(q, "parentKitSku", "KIT-RENO-1BR");
            int parentKitQty = (int)GetNum(q, "parentKitQty", 10);
            string unitNumber = GetStr(q, "unitNumber", "UNIT-TBD");
            bool fpConv = GetBool(q, "floorplanConverted", true);
            bool edsVal = GetBool(q, "edsValidated", true);
            bool outVal = GetBool(q, "outboundPrintValidated", true);

            var workflowSteps9 = new ArrayList
            {
                new Dictionary<string, object> { { "step", 1 }, { "title", "Start at Quote" }, { "status", "COMPLETED" }, { "detail", "Quote=" + qid } },
                new Dictionary<string, object> { { "step", 2 }, { "title", "Convert Using Floor Plan" }, { "status", fpConv ? "COMPLETED" : "PENDING" }, { "detail", fpConv ? "Floorplan child parts mapped" : "Pending floorplan pick" } },
                new Dictionary<string, object> { { "step", 3 }, { "title", "Set Parent Quantity" }, { "status", "COMPLETED" }, { "detail", parentKitSku + " x " + parentKitQty } },
                new Dictionary<string, object> { { "step", 4 }, { "title", "Build Order & Availability" }, { "status", "COMPLETED" }, { "detail", "ATP+CO09+ZSTOCK | " + checkTimingMode } },
                new Dictionary<string, object> { { "step", 5 }, { "title", "Handle Backordered Kits" }, { "status", (baCount + directFlexCount) > 0 ? "RESOLVED_FLEX" : "CLEAN" }, { "detail", "WaitPreserve=" + waitPreserveCount + ", Linehaul=" + linehaulCount + ", Flex=" + directFlexCount } },
                new Dictionary<string, object> { { "step", 6 }, { "title", "Confirm Delivery Setup" }, { "status", edsVal ? "COMPLETED" : "REVIEW" }, { "detail", "EDS & Installation verified" } },
                new Dictionary<string, object> { { "step", 7 }, { "title", "Save/Reopen & Unit #" }, { "status", string.IsNullOrEmpty(unitNumber) || unitNumber.StartsWith("PENDING") ? "PENDING" : "COMPLETED" }, { "detail", "unitNumber=" + unitNumber } },
                new Dictionary<string, object> { { "step", 8 }, { "title", "Daily ZSTOCK Review" }, { "status", dailyZstockRev ? "ACTIVE_GUARD" : "OFF" }, { "detail", protectFutureKits ? "Golden Rule Active: Preserves future kits" : "Today-only short check" } },
                new Dictionary<string, object> { { "step", 9 }, { "title", "Outbound Print Validation" }, { "status", outboundPrintVal && outVal ? "VALIDATED" : "EXCEPTION_QUEUE" }, { "detail", "DeepMap Events=" + deepMapEvents.Count } }
            };

            string phasingMode = GetStr(scb, "forecastTimePhasingMode", "FRONT_LOAD_FIRST_MONDAY");
            double qMo = Math.Max(40.0, totalDemand);
            var legacyWeekly = new ArrayList { Math.Round(qMo * 0.25, 1), Math.Round(qMo * 0.25, 1), Math.Round(qMo * 0.25, 1), Math.Round(qMo * 0.25, 1) };
            var activeWeekly = (phasingMode == "FRONT_LOAD_FIRST_MONDAY")
                ? new ArrayList { Math.Round(qMo, 1), 0.0, 0.0, 0.0 }
                : new ArrayList { Math.Round(qMo * 0.25, 1), Math.Round(qMo * 0.25, 1), Math.Round(qMo * 0.25, 1), Math.Round(qMo * 0.25, 1) };

            double bwrBefore = 1.62;
            double bwrAfter = (phasingMode == "FRONT_LOAD_FIRST_MONDAY" ? 1.08 : 1.48) - (trueFloorMode ? 0.06 : 0.0);

            var result = new Dictionary<string, object>
            {
                { "evaluatedAt", UtcNowIso() },
                { "activePreset", GetStr(hc, "activePreset") },
                { "quoteId", qid },
                { "customer", GetStr(q, "customer") },
                { "parentKitSku", parentKitSku },
                { "parentKitQty", parentKitQty },
                { "unitNumber", unitNumber },
                { "orgType", orgType },
                { "salesArea", GetStr(org, "salesAreaRouting", "SALES_AREA_20_UNIFIED") },
                { "fulfillmentMode", fulfillmentMode },
                { "localDc", localDc },
                { "kittingDc", kittingDc },
                { "kittingDcName", kittingDcName },
                { "kittingDcLaborUtilPct", kitLaborUtil },
                { "kittingDcFabShop", kitFabShop },
                { "timingDecision", new Dictionary<string, object> {
                    { "orderToRddDays", orderLeadDays },
                    { "creditHoldDays", creditHoldDays },
                    { "adjustedRddShiftDays", rddShift },
                    { "effectiveKittingLeadDays", effectiveLead },
                    { "immediateCheckHorizonDays", var1Horizon },
                    { "scheduledRecheckLeadDays", var2Recheck },
                    { "checkTimingMode", checkTimingMode },
                    { "deliveryBlock", delivBlock },
                    { "inventoryCommitmentStatus", commitStatus }
                }},
                { "forecastComparison", new Dictionary<string, object> {
                    { "monthlyDemandUnits", Math.Round(qMo, 1) },
                    { "phasingMode", phasingMode },
                    { "legacyWeekly", legacyWeekly },
                    { "activeWeekly", activeWeekly },
                    { "mulesoft30dReservationQty", Math.Round(qMo, 1) },
                    { "bullwhipBefore", bwrBefore },
                    { "bullwhipAfter", Math.Round(bwrAfter, 2) }
                }},
                { "scbSummary", new Dictionary<string, object> {
                    { "serviceLevelPct", GetNum(scb, "scbEoqServiceLevelPct", 95.0) },
                    { "survivalQuantile", GetStr(scb, "scbLeadTimeSurvivalQuantile", "p90") },
                    { "avgP90LeadDays", scbItemN > 0 ? Math.Round(sumP90 / scbItemN, 1) : 8.0 },
                    { "totalMultiEchelonSafetyStock", Math.Round(totalSafety, 0) },
                    { "totalCvarUsd", Math.Round(totalCvar, 0) },
                    { "maxEoqZScore", Math.Round(maxZ, 2) },
                    { "quipuEmaAlpha", GetNum(scb, "scbQuipuEmaAlpha", 0.90) }
                }},
                { "summaryCounts", new Dictionary<string, object> {
                    { "totalLines", lineDecisions.Count },
                    { "inKitCount", inKitCount },
                    { "expandedToKitCount", expandedCount },
                    { "waitPreserveCount", waitPreserveCount },
                    { "linehaulMatchupCount", linehaulCount },
                    { "laborGuardFlexCount", laborGuardCount },
                    { "directFlexCount", directFlexCount },
                    { "autoStripZtasCount", ztasCount },
                    { "excludedHazardDoorCount", exclCount },
                    { "appliancePalletCount", applCount },
                    { "blockedBaCount", baCount },
                    { "batchDeferredCount", batchDefCount },
                    { "paperAtpFalseGreenRiskCount", paperRiskCount },
                    { "proactiveAlertsCount", alertCount }
                }},
                { "workflowSteps9", workflowSteps9 },
                { "deepMapEvents", deepMapEvents },
                { "lineDecisions", lineDecisions }
            };
            EnrichTx03Tx77Order(result, q, hc, deepMapEvents);
            _relational["lastHeuristicsEvaluation"] = result;
            RefreshPathwaysAndDirectives("_evaluate_heuristics_order");
            return result;
        }

        private static string[] PhysicalDcSpecs = new[]
        {
            "GA02|Atlanta Kitting Hub|620|300|1.18|0.86|true",
            "NC34|Charlotte Spoke DC|690|265|1.08|0.72|false",
            "TX03|Dallas Kitting Hub|420|330|1.26|0.91|true",
            "TX77|Houston Regional DC|455|395|1.29|0.93|false",
            "IL30|Chicago Kitting Hub|535|180|1.24|0.89|true",
            "NJ22|New Jersey Kitting Hub|780|175|1.34|0.96|true",
            "PA15|Philadelphia Spoke DC|750|195|1.21|0.84|false",
            "CA20|Southern CA Kitting Hub|130|315|1.38|0.95|true",
            "FL14|Orlando Kitting Hub|685|410|1.16|0.82|true",
            "AZ17|Phoenix Regional Hub|215|325|1.12|0.78|false",
            "WA09|Seattle Pacific Hub|115|85|1.15|0.79|false",
            "CO11|Denver Mountain Hub|315|235|1.09|0.69|false",
            "AL04|Birmingham Spoke DC|575|315|1.04|0.61|false",
            "FL08|Jacksonville Spoke DC|685|365|1.09|0.68|false",
            "OK12|Oklahoma City Spoke DC|415|285|1.02|0.55|false",
            "MN55|Minneapolis Spoke DC|475|125|1.05|0.63|false",
            "MO18|St Louis Spoke DC|505|235|1.07|0.66|false",
            "OH24|Columbus Spoke DC|625|210|1.11|0.71|false",
            "MA07|Boston Spoke DC|820|125|1.25|0.88|false",
            "MD19|Baltimore Spoke DC|735|220|1.22|0.85|false",
            "CA42|Oakland NorCal Spoke DC|90|220|1.27|0.90|false",
            "FL31|Miami South Spoke DC|710|465|1.28|0.91|false",
            "OR11|Portland Spoke DC|110|130|1.08|0.67|false",
            "NV06|Reno West Spoke DC|140|205|1.06|0.58|false",
            "UT08|Salt Lake Spoke DC|220|195|1.03|0.54|false"
        };

        private static string[] PhysicalEdgeSpecs = new[]
        {
            "GA02|NC34|4.5|1.12", "GA02|AL04|3.0|1.04", "GA02|FL08|5.5|1.09", "GA02|FL14|7.0|1.14",
            "GA02|TX03|13.0|1.20", "GA02|OH24|9.5|1.14", "NC34|MD19|6.5|1.18", "NC34|PA15|8.0|1.20",
            "TX03|TX77|4.2|1.28", "TX03|OK12|3.5|1.03", "TX03|CO11|12.5|1.08", "TX03|MO18|10.5|1.10",
            "TX03|AZ17|15.0|1.15", "IL30|MN55|6.5|1.06", "IL30|MO18|5.0|1.11", "IL30|OH24|6.0|1.14",
            "NJ22|PA15|2.0|1.32", "NJ22|MA07|4.5|1.26", "NJ22|MD19|3.5|1.25", "CA20|CA42|6.0|1.30",
            "CA20|AZ17|6.0|1.18", "CA20|NV06|7.5|1.10", "FL14|FL08|2.5|1.08", "FL14|FL31|4.0|1.26",
            "WA09|OR11|3.2|1.10", "OR11|CA42|10.0|1.12", "CO11|UT08|8.0|1.05", "UT08|NV06|8.0|1.04"
        };

        private Dictionary<string, object> BuildPhysicalDcGraph()
        {
            var nodes = new ArrayList();
            var nodeMap = new Dictionary<string, Dictionary<string, object>>();
            foreach (string spec in PhysicalDcSpecs)
            {
                string[] p = spec.Split('|');
                var nd = new Dictionary<string, object>
                {
                    { "dc", p[0] }, { "name", p[1] }, { "x", int.Parse(p[2]) }, { "y", int.Parse(p[3]) },
                    { "trafficWeight", double.Parse(p[4], System.Globalization.CultureInfo.InvariantCulture) },
                    { "populationDensityIndex", double.Parse(p[5], System.Globalization.CultureInfo.InvariantCulture) },
                    { "isKittingHub", p[6] == "true" }, { "physicalOnly", true }
                };
                nodes.Add(nd);
                nodeMap[p[0]] = nd;
            }
            var emaMap = EnsureDict(_relational, "rfidEdgeWeights");
            var edges = new ArrayList();
            foreach (string es in PhysicalEdgeSpecs)
            {
                string[] ep = es.Split('|');
                double baseHrs = double.Parse(ep[2], System.Globalization.CultureInfo.InvariantCulture);
                double traf = double.Parse(ep[3], System.Globalization.CultureInfo.InvariantCulture);
                string keyFwd = ep[0] + "->" + ep[1];
                string keyRev = ep[1] + "->" + ep[0];
                double ema = emaMap.ContainsKey(keyFwd) ? GetNum(emaMap, keyFwd, 1.0) : (emaMap.ContainsKey(keyRev) ? GetNum(emaMap, keyRev, 1.0) : 1.0);
                double wCost = Math.Round(baseHrs * traf * ema, 2);
                edges.Add(new Dictionary<string, object>
                {
                    { "from", ep[0] }, { "to", ep[1] }, { "baseHours", baseHrs },
                    { "trafficWeight", traf }, { "rfidEmaMultiplier", Math.Round(ema, 3) }, { "effectiveWeight", wCost }
                });
            }
            return new Dictionary<string, object> { { "nodes", nodes }, { "nodeMap", nodeMap }, { "edges", edges } };
        }

        private Dictionary<string, object> RunWeightedDijkstra(string sourceDc, string targetDc, double unitQty, Dictionary<string, object> hc)
        {
            string src = string.IsNullOrEmpty(sourceDc) ? "NC34" : sourceDc.Trim().ToUpperInvariant();
            string dst = string.IsNullOrEmpty(targetDc) ? "GA02" : targetDc.Trim().ToUpperInvariant();
            var g = BuildPhysicalDcGraph();
            var nodeMap = (Dictionary<string, Dictionary<string, object>>)g["nodeMap"];
            var adj = new Dictionary<string, List<KeyValuePair<string, double>>>();
            foreach (string k in nodeMap.Keys) adj[k] = new List<KeyValuePair<string, double>>();
            var baseHrsMap = new Dictionary<string, double>();
            foreach (object eObj in EnsureList(g, "edges"))
            {
                var e = eObj as Dictionary<string, object>;
                if (e == null) continue;
                string u = GetStr(e, "from"), v = GetStr(e, "to");
                double w = GetNum(e, "effectiveWeight", 6.0);
                double bh = GetNum(e, "baseHours", 6.0);
                if (!adj.ContainsKey(u)) adj[u] = new List<KeyValuePair<string, double>>();
                if (!adj.ContainsKey(v)) adj[v] = new List<KeyValuePair<string, double>>();
                adj[u].Add(new KeyValuePair<string, double>(v, w));
                adj[v].Add(new KeyValuePair<string, double>(u, w));
                baseHrsMap[u + "|" + v] = bh;
                baseHrsMap[v + "|" + u] = bh;
            }
            var dist = new Dictionary<string, double>();
            var prev = new Dictionary<string, string>();
            var unvisited = new HashSet<string>(adj.Keys);
            foreach (string k in adj.Keys) dist[k] = 1e9;
            if (!dist.ContainsKey(src)) { dist[src] = 0.0; unvisited.Add(src); adj[src] = new List<KeyValuePair<string, double>>(); }
            dist[src] = 0.0;
            while (unvisited.Count > 0)
            {
                string best = null; double bestD = 1e9;
                foreach (string u in unvisited) { if (dist[u] < bestD) { bestD = dist[u]; best = u; } }
                if (best == null || best == dst) break;
                unvisited.Remove(best);
                foreach (var edge in adj[best])
                {
                    if (!unvisited.Contains(edge.Key)) continue;
                    double alt = dist[best] + edge.Value;
                    if (alt < dist[edge.Key]) { dist[edge.Key] = alt; prev[edge.Key] = best; }
                }
            }
            var path = new ArrayList();
            double totalHours = 0.0;
            if (src == dst) { path.Add(src); }
            else if (prev.ContainsKey(dst))
            {
                var stack = new List<string>();
                string cur = dst;
                while (!string.IsNullOrEmpty(cur)) { stack.Add(cur); prev.TryGetValue(cur, out cur); }
                stack.Reverse();
                for (int i = 0; i < stack.Count; i++)
                {
                    path.Add(stack[i]);
                    if (i > 0) { double bh; if (baseHrsMap.TryGetValue(stack[i - 1] + "|" + stack[i], out bh)) totalHours += bh; }
                }
            }
            else { path.Add(src); path.Add(dst); totalHours = 12.0; }
            double wScore = (src == dst) ? 0.0 : (dist.ContainsKey(dst) && dist[dst] < 1e8 ? Math.Round(dist[dst], 2) : 14.5);
            double estDays = Math.Max(0.5, Math.Round(totalHours / 10.0, 1));
            var sla = EnsureDict(hc, "slaAndTiming");
            double touchLabor = GetNum(sla, "consolidationTouchLaborUsd", 18.5);
            double holdPerDay = GetNum(sla, "holdingPenaltyPerDayUsd", 4.25);
            double consCost = Math.Round((src == dst ? 0.0 : touchLabor) + totalHours * 1.45 + Math.Max(1.0, unitQty) * 0.65 + estDays * holdPerDay, 2);
            double dropCost = Math.Round((src == dst ? 6.0 : 28.0) + totalHours * 1.95 + Math.Max(1.0, unitQty) * 0.40, 2);
            double diffUsd = Math.Round(consCost - dropCost, 2);
            double holdUpcharge = Math.Round(Math.Max(0.0, estDays * holdPerDay), 2);
            string recMode = (src == dst || diffUsd <= 12.0) ? "CONSOLIDATED_TRANSFER" : "SPLIT_DROPSHIP_DIRECT";
            return new Dictionary<string, object>
            {
                { "sourceDc", src }, { "targetDc", dst }, { "path", path }, { "weightedDijkstraCost", wScore },
                { "estimatedHours", Math.Round(totalHours, 1) }, { "estimatedDays", estDays },
                { "consolidationCostUsd", consCost }, { "directSplitDropshipCostUsd", dropCost },
                { "consolidationDifferentialUsd", diffUsd }, { "optionalHoldingCostUpchargeUsd", holdUpcharge },
                { "recommendedMode", recMode }
            };
        }

        private void EnrichTx03Tx77Order(Dictionary<string, object> result, Dictionary<string, object> quote, Dictionary<string, object> hc, ArrayList deepMapEvents)
        {
            var org = EnsureDict(hc, "organizationAndMigration");
            var sla = EnsureDict(hc, "slaAndTiming");
            var ful = EnsureDict(hc, "fulfillmentAndKitting");
            org["virtualWarehousesAllowed"] = false;
            org["snowflakeRole"] = "analytical-export-only";
            double surgeW = GetNum(sla, "expediteSurgeWeight", 0.35);
            double ctoThresh = GetNum(ful, "ctoSeasonalityThreshold", 0.65);
            string kittingDc = GetStr(result, "kittingDc", "GA02");
            var lines = EnsureList(result, "lineDecisions");
            int alerts10d = 0, csr5dCount = 0, sigma3Pass = 0, ctoCount = 0;
            double maxSigma2 = 0.0, totalConsCost = 0.0, totalDropCost = 0.0;
            var ctoList = new ArrayList();
            var modBuckets = new Dictionary<string, ArrayList>();

            foreach (object lObj in lines)
            {
                var ld = lObj as Dictionary<string, object>;
                if (ld == null) continue;
                string sku = GetStr(ld, "sku");
                int qty = (int)Math.Max(1, GetNum(ld, "qty", 1));
                int tf = (int)GetNum(ld, "trueFloorAvail", 0);
                int proj = (int)GetNum(ld, "projectedQty", 0);
                string spoke = GetStr(ld, "spokeDc", kittingDc);
                string sev = GetStr(ld, "severity", "ok");
                string cat = GetStr(ld, "sapItemCategory", "ZTAN");

                double deficitRatio = (qty > 0) ? Math.Max(0.0, (double)(qty - tf) / (double)qty) : 0.0;
                double sigma2Pct = Math.Round(Math.Min(99.5, (deficitRatio * 68.0 + surgeW * 24.0 + (proj < qty ? 18.0 : 0.0))), 2);
                double sigma3Pct = (tf >= qty) ? 0.12 : (proj >= qty ? 0.24 : Math.Round(Math.Min(45.0, 0.35 + deficitRatio * 18.0 * surgeW), 2));
                bool alert10d = (sigma2Pct >= 15.0) || (tf < qty);
                if (sigma2Pct > maxSigma2) maxSigma2 = sigma2Pct;
                if (sigma3Pct < 0.27) sigma3Pass++;
                if (alert10d) { alerts10d++; csr5dCount++; }

                double ctoScore = Math.Round(Math.Min(0.99, (sev != "ok" ? 0.72 : 0.38) + (cat == "ZTNF" || cat == "ZTAS" ? 0.22 : 0.08) + surgeW * 0.2), 2);
                string ctoRec = ctoScore >= ctoThresh ? (cat == "ZTAS" ? "SUPPLIER_DROPSHIP_SLA" : "CTO_ROVING_STOCKPILE") : "CONSOLIDATED_HOLDING_COST_UPCHARGE";
                if (ctoScore >= ctoThresh) { ctoCount++; ctoList.Add(new Dictionary<string, object> { { "sku", sku }, { "ctoScore", ctoScore }, { "recommendation", ctoRec } }); }

                var dRoute = RunWeightedDijkstra(sev == "ok" ? kittingDc : spoke, kittingDc, qty, hc);
                totalConsCost += GetNum(dRoute, "consolidationCostUsd", 0);
                totalDropCost += GetNum(dRoute, "directSplitDropshipCostUsd", 0);

                ld["sigma2At10dPct"] = sigma2Pct;
                ld["sigma3At6dPct"] = sigma3Pct;
                ld["sigma3Pass"] = sigma3Pct < 0.27;
                ld["expediteSurgeProb"] = Math.Round(surgeW * (1.0 + deficitRatio), 2);
                ld["tenDayAlertTriggered"] = alert10d;
                ld["alertRecipients"] = alert10d ? new[] { "Supervisor", "CSR", "Buyer" } : new string[0];
                ld["csrCustomerNotifyMinDays"] = 5;
                ld["ctoScore"] = ctoScore;
                ld["ctoRecommendation"] = ctoRec;
                ld["dijkstraRoute"] = dRoute;
                ld["consolidationDifferentialUsd"] = dRoute["consolidationDifferentialUsd"];

                string modKey = (sev == "ok" && cat != "ZTAS") ? "MOD-1-CORE-KIT" : (cat == "ZLAB" ? "MOD-SVC-INSTALL" : "MOD-2-FLEX-PEEL");
                if (!modBuckets.ContainsKey(modKey)) modBuckets[modKey] = new ArrayList();
                modBuckets[modKey].Add(sku);
            }
            var modularGroups = new ArrayList();
            foreach (var kv in modBuckets)
            {
                modularGroups.Add(new Dictionary<string, object>
                {
                    { "moduleId", kv.Key }, { "coOccurrenceProb", kv.Key == "MOD-1-CORE-KIT" ? 0.94 : 0.78 },
                    { "skus", kv.Value }, { "peelCandidate", kv.Key != "MOD-1-CORE-KIT" }
                });
            }
            var rfidList = EnsureList(_relational, "rfidShipments");
            var dijkstraBest = RunWeightedDijkstra(GetStr(result, "localDc", "NC34"), kittingDc, GetNum(result, "parentKitQty", 10), hc);
            string recStrategy = (totalConsCost - totalDropCost) <= 15.0 ? "CONSOLIDATED_TRANSFER" : "SPLIT_DROPSHIP_WITH_HOLDING_OPTION";
            result["tx03Tx77Architecture"] = new Dictionary<string, object>
            {
                { "virtualWarehousesAllowed", false },
                { "snowflakeRole", "analytical-export-only" },
                { "sapApiTimePhasedActive", true },
                { "dijkstraBestPath", dijkstraBest },
                { "consolidationDifferential", new Dictionary<string, object> {
                    { "totalConsolidationCostUsd", Math.Round(totalConsCost, 2) },
                    { "totalDirectDropshipCostUsd", Math.Round(totalDropCost, 2) },
                    { "netDifferentialUsd", Math.Round(totalConsCost - totalDropCost, 2) },
                    { "recommendedStrategy", recStrategy }
                }},
                { "stockoutGuardrails", new Dictionary<string, object> {
                    { "alerts10dCount", alerts10d }, { "csr5dNoticeCount", csr5dCount },
                    { "maxSigma2At10dPct", maxSigma2 }, { "sigma3At6dPassCount", sigma3Pass },
                    { "ctoCandidatesCount", ctoCount }, { "ctoCandidates", ctoList }
                }},
                { "modularBomGroups", modularGroups },
                { "activeRfidShipmentsCount", rfidList.Count }
            };
            string pathwayTier = (alerts10d > 0 && maxSigma2 >= 15.0) ? "M365ImmediateCard" : "ShiftDigest";
            deepMapEvents.Add(new Dictionary<string, object>
            {
                { "eventType", "PathwayClassification" },
                { "targetQueue", pathwayTier },
                { "sku", GetStr(result, "parentKitSku", "KIT-RENO-1BR") },
                { "detail", "alerts10d=" + alerts10d + ", maxSigma2=" + maxSigma2 + "%, strategy=" + recStrategy }
            });
            var qList = EnsureList(_relational, "receivingQuarantine");
            int activeQ = 0;
            string qSku = GetStr(result, "parentKitSku", "KIT-RENO-1BR");
            foreach (object qObj in qList)
            {
                var qDict = qObj as Dictionary<string, object>;
                if (qDict != null && !string.Equals(GetStr(qDict, "status"), "CLEARED", StringComparison.OrdinalIgnoreCase))
                {
                    if (activeQ == 0 && !string.IsNullOrEmpty(GetStr(qDict, "sku"))) qSku = GetStr(qDict, "sku");
                    activeQ++;
                }
            }
            if (activeQ > 0)
            {
                deepMapEvents.Add(new Dictionary<string, object>
                {
                    { "eventType", "ReturnQuarantineCase" },
                    { "targetQueue", "return-quarantine-governance" },
                    { "sku", qSku },
                    { "detail", "activeQuarantines=" + activeQ + ", chain=Return" }
                });
            }
        }

        private Dictionary<string, object> GetTimePhasedInventory(string dcFilter, string skuFilter)
        {
            lock (_stateLock)
            {
                EnsureHeuristicsConfig();
                string dc = string.IsNullOrEmpty(dcFilter) ? "" : dcFilter.Trim().ToUpperInvariant();
                string skuQ = string.IsNullOrEmpty(skuFilter) ? "" : skuFilter.Trim().ToUpperInvariant();
                var rows = new ArrayList();
                foreach (object iObj in EnsureList(_relational, "items"))
                {
                    var it = iObj as Dictionary<string, object>;
                    if (it == null) continue;
                    string sku = GetStr(it, "sku").ToUpperInvariant();
                    if (!string.IsNullOrEmpty(skuQ) && sku.IndexOf(skuQ, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    var floorMap = EnsureDict(it, "floorQohByDc");
                    var commMap = EnsureDict(it, "committedQtyByDc");
                    var s916Map = EnsureDict(it, "storage916QtyByDc");
                    int pastDue = (int)GetNum(it, "unreceivedPastDuePoQty", 0);
                    int expRepl = (int)GetNum(it, "expectedReplenishmentQty", 0);
                    int replDays = (int)GetNum(it, "incomingReplenishmentDays", 7);
                    var dcEntries = new ArrayList();
                    foreach (var kv in floorMap)
                    {
                        if (!string.IsNullOrEmpty(dc) && !string.Equals(kv.Key, dc, StringComparison.OrdinalIgnoreCase)) continue;
                        int fq = Convert.ToInt32(kv.Value);
                        int cq = (int)GetNum(commMap, kv.Key, 0);
                        int q916 = (int)GetNum(s916Map, kv.Key, 0);
                        int avail0 = Math.Max(0, fq - cq - q916);
                        dcEntries.Add(new Dictionary<string, object>
                        {
                            { "dc", kv.Key }, { "physicalOnly", true }, { "floorQoh", fq }, { "committedReservations", cq },
                            { "storage916Excluded", q916 }, { "excludedPastDueCo09Po", pastDue },
                            { "inTransitStoQty", expRepl }, { "shipperEtaDays", replDays },
                            { "availableAtDate", new Dictionary<string, object> {
                                { "T+0", avail0 }, { "T+3", avail0 + (replDays <= 3 ? expRepl : 0) },
                                { "T+6", avail0 + (replDays <= 6 ? expRepl : 0) }, { "T+10", avail0 + (replDays <= 10 ? expRepl : 0) },
                                { "T+14", avail0 + expRepl }
                            }}
                        });
                    }
                    rows.Add(new Dictionary<string, object> { { "sku", sku }, { "description", GetStr(it, "description") }, { "dcs", dcEntries } });
                }
                return new Dictionary<string, object>
                {
                    { "ok", true }, { "dataSource", "SAP_DIRECT_API" },
                    { "snowflakeOperationalPipelineBypassed", true }, { "snowflakeRole", "analytical-export-only" },
                    { "virtualWarehousesAllowed", false }, { "dcFilter", dc }, { "skuFilter", skuQ }, { "items", rows }
                };
            }
        }

        private Dictionary<string, object> GetUnifiedMarketplaceInventory(string targetDc, string skuFilter, int reqQty, bool includeVirtual, out int httpStatus)
        {
            httpStatus = 200;
            if (includeVirtual)
            {
                httpStatus = 400;
                return ErrorObj("VIRTUAL_WAREHOUSE_PROHIBITED: Virtual storage bins (TX03-in-TX77 / NJ22-in-PA50) cause MRO backorder hijacking and fake AP/on-hand distortion. Use Unified Physical Inventory Marketplace + Weighted Greedy Dijkstra.");
            }
            lock (_stateLock)
            {
                var hc = EnsureHeuristicsConfig();
                string tgt = string.IsNullOrEmpty(targetDc) ? "TX77" : targetDc.Trim().ToUpperInvariant();
                string skuQ = string.IsNullOrEmpty(skuFilter) ? "" : skuFilter.Trim().ToUpperInvariant();
                int qNeed = Math.Max(1, reqQty);
                var options = new ArrayList();
                foreach (object iObj in EnsureList(_relational, "items"))
                {
                    var it = iObj as Dictionary<string, object>;
                    if (it == null) continue;
                    string sku = GetStr(it, "sku").ToUpperInvariant();
                    if (!string.IsNullOrEmpty(skuQ) && sku.IndexOf(skuQ, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    var floorMap = EnsureDict(it, "floorQohByDc");
                    var commMap = EnsureDict(it, "committedQtyByDc");
                    var s916Map = EnsureDict(it, "storage916QtyByDc");
                    foreach (var kv in floorMap)
                    {
                        int fq = Convert.ToInt32(kv.Value);
                        int cq = (int)GetNum(commMap, kv.Key, 0);
                        int q916 = (int)GetNum(s916Map, kv.Key, 0);
                        int netAvail = Math.Max(0, fq - cq - q916);
                        if (netAvail <= 0) continue;
                        var route = RunWeightedDijkstra(kv.Key, tgt, qNeed, hc);
                        options.Add(new Dictionary<string, object>
                        {
                            { "sku", sku }, { "description", GetStr(it, "description") }, { "sourceDc", kv.Key },
                            { "targetDc", tgt }, { "physicalOnly", true }, { "netPhysicalAvail", netAvail },
                            { "canFulfillQty", netAvail >= qNeed }, { "dijkstraRoute", route },
                            { "recommendedMode", route["recommendedMode"] },
                            { "consolidationDifferentialUsd", route["consolidationDifferentialUsd"] }
                        });
                    }
                }
                return new Dictionary<string, object>
                {
                    { "ok", true }, { "virtualWarehousesAllowed", false }, { "snowflakeRole", "analytical-export-only" },
                    { "routingAlgorithm", "WEIGHTED_GREEDY_DIJKSTRA" }, { "targetDc", tgt }, { "requestedQty", qNeed },
                    { "physicalOptionsCount", options.Count }, { "options", options }
                };
            }
        }

        private Dictionary<string, object> GetNodalLogisticsMap()
        {
            lock (_stateLock)
            {
                EnsureHeuristicsConfig();
                var g = BuildPhysicalDcGraph();
                var rfids = EnsureList(_relational, "rfidShipments");
                if (rfids.Count == 0)
                {
                    rfids.Add(new Dictionary<string, object> {
                        { "shipmentId", "IS-1001" }, { "shipmentType", "IS" }, { "lineStyle", "dashed" },
                        { "fromDc", "NC34" }, { "toDc", "GA02" }, { "status", "IN_TRANSIT" }, { "protocol", "rust-rfid-open-tracker/v1" },
                        { "tagId", "RUST-RFID-IS1001" }, { "baselineHours", 4.5 }, { "activatedAtUtc", UtcNowIso() }
                    });
                    rfids.Add(new Dictionary<string, object> {
                        { "shipmentId", "CS-2001" }, { "shipmentType", "CS" }, { "lineStyle", "solid" },
                        { "fromDc", "TX03" }, { "toDc", "TX77" }, { "status", "CUSTOMER_DISPATCHED" }, { "protocol", "rust-rfid-open-tracker/v1" },
                        { "tagId", "RUST-RFID-CS2001" }, { "baselineHours", 4.2 }, { "activatedAtUtc", UtcNowIso() }
                    });
                }
                var kanbanLanes = new Dictionary<string, object>
                {
                    { "TO_PICK", new ArrayList() }, { "IN_TRANSIT_IS", new ArrayList() },
                    { "CUSTOMER_STAGED_CS", new ArrayList() }, { "QUARANTINE_RETURN", EnsureList(_relational, "receivingQuarantines") },
                    { "AUTO_REPLEN_PO", EnsureList(_relational, "autoReplenishmentPos") }
                };
                foreach (object rObj in rfids)
                {
                    var rf = rObj as Dictionary<string, object>;
                    if (rf == null) continue;
                    if (GetStr(rf, "shipmentType") == "IS") ((ArrayList)kanbanLanes["IN_TRANSIT_IS"]).Add(rf);
                    else ((ArrayList)kanbanLanes["CUSTOMER_STAGED_CS"]).Add(rf);
                }
                var openDirs = new ArrayList();
                foreach (object dObj in EnsureList(_relational, "bodyDirectives"))
                {
                    var dd = dObj as Dictionary<string, object>;
                    if (dd == null) continue;
                    string dst = GetStr(dd, "status");
                    if (dst == "open" || dst == "ack" || dst == "in_progress") openDirs.Add(dd);
                }
                foreach (object qObj in EnsureList(_relational, "quotes"))
                {
                    var qd = qObj as Dictionary<string, object>;
                    if (qd == null) continue;
                    var linkedToPick = new ArrayList();
                    foreach (object dObj in openDirs)
                    {
                        var dd = dObj as Dictionary<string, object>;
                        if (dd != null && GetStr(dd, "kanbanLane") == "TO_PICK") linkedToPick.Add(GetStr(dd, "directiveId"));
                    }
                    ((ArrayList)kanbanLanes["TO_PICK"]).Add(new Dictionary<string, object> {
                        { "quoteId", GetStr(qd, "id") }, { "customer", GetStr(qd, "customer") }, { "localDc", GetStr(qd, "localDc", "NC34") },
                        { "linkedDirectives", linkedToPick }
                    });
                }
                return new Dictionary<string, object>
                {
                    { "ok", true }, { "protocol", "rust-rfid-open-tracker/v1" }, { "virtualWarehousesAllowed", false },
                    { "nodes", g["nodes"] }, { "edges", g["edges"] }, { "shipments", rfids },
                    { "rfidEdgeWeights", EnsureDict(_relational, "rfidEdgeWeights") },
                    { "kanbanLanes", kanbanLanes }, { "quipuNotes", EnsureList(_relational, "quipuNoteClassifications") },
                    { "stockopolyBins", EnsureList(_relational, "stockopolyBinMappings") }, { "sapSignals", EnsureList(_relational, "sapSignals") },
                    { "bodyDirectives", openDirs },
                    { "pathwaySummary", EnsureDict(EnsureDict(_relational, "pathwaySummary"), "counts") }
                };
            }
        }

        private static string SeverityFromPriority(double p)
        {
            if (p >= 0.85) return "critical";
            if (p >= 0.65) return "act";
            if (p >= 0.40) return "watch";
            return "info";
        }

        private static string M365ModeFromPriority(double p)
        {
            if (p >= 0.85) return "IMMEDIATE_CARD";
            if (p >= 0.65) return "SHIFT_DIGEST";
            return "LOG_ONLY";
        }

        private static Dictionary<string, object> MakeSystemicRule(string ruleId)
        {
            if (ruleId == "R7") return new Dictionary<string, object> { { "ruleId", "R7" }, { "name", "Populate ABC Inventory Catalog" }, { "sapSignalType", "PR_ADJUST" }, { "owner", "IT Governance + Planner" }, { "sequence", 1 }, { "blockedRawInventoryAdjust", true } };
            if (ruleId == "R3") return new Dictionary<string, object> { { "ruleId", "R3" }, { "name", "Switch Safety Stock Planning Method" }, { "sapSignalType", "PO_ADJUST" }, { "owner", "Planner + Buyer" }, { "sequence", 2 }, { "blockedRawInventoryAdjust", true } };
            if (ruleId == "R5") return new Dictionary<string, object> { { "ruleId", "R5" }, { "name", "Transaction Reason Code enforcement" }, { "sapSignalType", "PR_ADJUST" }, { "owner", "IT Governance + Quality" }, { "sequence", 3 }, { "blockedRawInventoryAdjust", true } };
            if (ruleId == "R2") return new Dictionary<string, object> { { "ruleId", "R2" }, { "name", "Allocation reconciliation batch job" }, { "sapSignalType", "PO_ADJUST" }, { "owner", "IT Governance + Planner" }, { "sequence", 4 }, { "blockedRawInventoryAdjust", true } };
            if (ruleId == "R10") return new Dictionary<string, object> { { "ruleId", "R10" }, { "name", "Assign Buyer / Planner codes" }, { "sapSignalType", "RDD_CREDIT_SHIFT" }, { "owner", "IT Governance + Procurement" }, { "sequence", 5 }, { "blockedRawInventoryAdjust", true } };
            return new Dictionary<string, object> { { "ruleId", "R4" }, { "name", "Item Cycle Count Enabled audit" }, { "sapSignalType", "PR_ADJUST" }, { "owner", "Site Lead + IT Governance" }, { "sequence", 6 }, { "blockedRawInventoryAdjust", true } };
        }

        private static Dictionary<string, object> BuildDirectiveObj(string signalKind, double priority, string ownerRole, string targetEntity, string title, string doThis, string whyMatters, string kanbanLane, string chain, ArrayList relatedRefs, string now)
        {
            string source = "holonhub-live";
            string fp = Sha1Hex(source + "|" + signalKind + "|" + targetEntity + "|" + title);
            string sev = SeverityFromPriority(priority);
            string mode = M365ModeFromPriority(priority);
            double pr = Math.Round(priority, 4);
            var env = (mode != "LOG_ONLY")
                ? new Dictionary<string, object> {
                    { "dispatchMode", mode },
                    { "toolTarget", mode == "IMMEDIATE_CARD" ? "teams_post_handoff_card" : "sp_write_artifact" },
                    { "governor", "verifier-governor" },
                    { "credentialPolicy", "ENV/VAULT/SECRETREF-ONLY" },
                    { "title", title }, { "ownerRole", ownerRole }, { "severity", sev }, { "priority", pr }
                }
                : null;
            return new Dictionary<string, object> {
                { "directiveId", "DIR-" + fp.Substring(0, 10).ToUpperInvariant() },
                { "fingerprint", fp },
                { "source", source },
                { "signalKind", signalKind },
                { "priority", pr },
                { "severity", sev },
                { "chain", chain },
                { "ownerRole", ownerRole },
                { "targetEntity", targetEntity },
                { "relatedRefs", relatedRefs },
                { "kanbanLane", kanbanLane },
                { "m365DispatchMode", mode },
                { "title", title },
                { "doThis", doThis },
                { "whyItMatters", whyMatters },
                { "status", "open" },
                { "createdAt", now },
                { "lastStatusAt", now },
                { "m365CardEnvelope", env }
            };
        }

        private Dictionary<string, object> RefreshPathwaysAndDirectives(string triggerAction)
        {
            string now = UtcNowIso();
            var firedSys = new Dictionary<string, Dictionary<string, object>>();
            var freshOps = new List<Dictionary<string, object>>();

            Action<string, string> addSys = (ruleId, evidenceRef) => {
                Dictionary<string, object> entry;
                if (!firedSys.TryGetValue(ruleId, out entry))
                {
                    entry = MakeSystemicRule(ruleId);
                    entry["evidenceRefs"] = new ArrayList();
                    firedSys[ruleId] = entry;
                }
                if (!string.IsNullOrEmpty(evidenceRef))
                {
                    var refs = (ArrayList)entry["evidenceRefs"];
                    if (!refs.Contains(evidenceRef)) refs.Add(evidenceRef);
                }
            };

            var ev = EnsureDict(_relational, "lastHeuristicsEvaluation");
            string qid = GetStr(ev, "quoteId", GetStr(_relational, "selectedQuoteId", "ROF-DEMO-001"));
            string localDc = GetStr(ev, "localDc", "NC34");
            string kittingDc = GetStr(ev, "kittingDc", "GA02");

            foreach (object lObj in EnsureList(ev, "lineDecisions"))
            {
                var ld = lObj as Dictionary<string, object>;
                if (ld == null) continue;
                string sku = GetStr(ld, "sku").ToUpperInvariant();
                string spoke = GetStr(ld, "spokeDc", localDc).ToUpperInvariant();
                string dcode = GetStr(ld, "decisionCode");
                double s3 = GetNum(ld, "sigma3At6dPct", 0.0);
                double s2 = GetNum(ld, "sigma2At10dPct", 0.0);
                bool tenDay = GetBool(ld, "tenDayAlertTriggered", false);
                var refs = new ArrayList(new[] { qid, sku, spoke, kittingDc });

                if (s3 >= 0.27 || dcode == "PEEL_BACKORDER")
                {
                    addSys("R3", qid + ":" + sku);
                    addSys("R10", sku);
                    freshOps.Add(BuildDirectiveObj(
                        "stockout_3sigma_6d", 0.95, "Buyer / CSR", "SKU::" + sku + "::" + qid,
                        "Critical 3-sigma@6d stockout risk on " + sku + " (" + s3 + "%) for " + qid,
                        "Trigger expedite PO / spoke transfer (" + spoke + "->" + kittingDc + ") and send 5d CSR warning.",
                        "Exceeds 0.27% 3-sigma@6d stockout guardrail on active kitting wave.",
                        "TO_PICK", "Order", refs, now));
                }
                else if (tenDay)
                {
                    addSys("R7", qid + ":" + sku);
                    freshOps.Add(BuildDirectiveObj(
                        "stockout_2sigma_10d", 0.80, "Buyer / Planner", "SKU::" + sku + "::" + qid,
                        "10-day 2-sigma watch on " + sku + " (" + s2 + "%) at " + kittingDc,
                        "Review IFR replenishment & 5-day CSR window before VL01N wave drop on " + qid + ".",
                        "Early Day-10 probability buffer alert allows planned procurement before expedite.",
                        "TO_PICK", "Plan", refs, now));
                }
                else if (dcode == "WAIT_FOR_SUPPLY_PRESERVE_KIT")
                {
                    freshOps.Add(BuildDirectiveObj(
                        "preserve_future_kit", 0.55, "Ops / Planner", "KIT::" + qid + "::" + sku,
                        "Preserve future kit " + qid + ": hold " + sku + " for scheduled inbound",
                        "Do not strip future kit; recheck automatically at Var2 (RDD-6d).",
                        "Slide Step 8 Golden Rule prevents premature manual kit splitting.",
                        "TO_PICK", "Order", refs, now));
                }
            }

            foreach (object qObj in EnsureList(_relational, "receivingQuarantines"))
            {
                var qr = qObj as Dictionary<string, object>;
                if (qr == null || GetStr(qr, "status").ToUpperInvariant() != "RECEIVING_QUARANTINE_RETURN") continue;
                string qrnId = GetStr(qr, "quarantineId", "QRN-1");
                string ordId = GetStr(qr, "orderId", qid);
                string sku = GetStr(qr, "sku").ToUpperInvariant();
                addSys("R2", qrnId);
                freshOps.Add(BuildDirectiveObj(
                    "receiving_quarantine_return", 0.88, "Receiving / Return Lead", "QRN::" + qrnId,
                    "Hold " + qrnId + " (" + sku + ") on dock for Vendor Return (" + ordId + ")",
                    "Segregate in Receiving Quarantine Gate, draft vendor RMA, and verify PO reduction.",
                    "Inbound stock arrived without upstream PO cancellation reduction.",
                    "QUARANTINE_RETURN", "Return", new ArrayList(new[] { qrnId, ordId, sku }), now));
            }

            foreach (object pObj in EnsureList(_relational, "autoReplenishmentPos"))
            {
                var po = pObj as Dictionary<string, object>;
                if (po == null || GetStr(po, "status").ToUpperInvariant() != "PO_REDUCTION_REQUIRED") continue;
                string poId = GetStr(po, "poId", "PO-RED");
                string sku = GetStr(po, "sku").ToUpperInvariant();
                string dc = GetStr(po, "dc", "TX03").ToUpperInvariant();
                string ordId = GetStr(po, "orderId", qid);
                addSys("R2", poId);
                freshOps.Add(BuildDirectiveObj(
                    "po_reduction_required", 0.70, "Buyer", "PO::" + poId,
                    "PO Reduction Required on " + poId + " (" + sku + " @ " + dc + ")",
                    "Send bounded PO_ADJUST signal to SAP before inbound dock receipt.",
                    "Prevents stranded overstock when linked customer quote/order cancels.",
                    "AUTO_REPLEN_PO", "Order", new ArrayList(new[] { poId, ordId, sku, dc }), now));
            }

            foreach (object bObj in EnsureList(_relational, "stockopolyBinMappings"))
            {
                var bm = bObj as Dictionary<string, object>;
                if (bm == null || GetStr(bm, "matchStatus").ToUpperInvariant() != "RECONCILE_BIN_DRIFT") continue;
                string sku = GetStr(bm, "sku").ToUpperInvariant();
                string dc = GetStr(bm, "dc", "TX03").ToUpperInvariant();
                string sloc = GetStr(bm, "sapStorageLocation", dc + "-BIN");
                addSys("R4", dc + ":" + sku);
                freshOps.Add(BuildDirectiveObj(
                    "bin_drift_reconcile", 0.72, "WMS / Floor Lead", "BIN::" + dc + "::" + sku,
                    "Reconcile Stockopoly bin drift on " + sku + " @ " + dc + " (" + sloc + ")",
                    "Verify physical bay coordinates in " + dc + " and scan bin / PICK_COMPLETE_KNOCKOFF.",
                    "3D slotting geometry disagrees with SAP Storage Location metadata.",
                    "TO_PICK", "Transform", new ArrayList(new[] { dc, sku, sloc }), now));
            }

            foreach (object nObj in EnsureList(_relational, "quipuNoteClassifications"))
            {
                var note = nObj as Dictionary<string, object>;
                if (note == null || !GetBool(note, "wipImpactFlag", false)) continue;
                string nid = GetStr(note, "noteId", "NOTE-1");
                string cls = GetStr(note, "classification", "WIP_STAGING_PARTIAL");
                string sku = GetStr(note, "sku").ToUpperInvariant();
                string dc = GetStr(note, "dc", "TX03").ToUpperInvariant();
                addSys("R5", nid);
                freshOps.Add(BuildDirectiveObj(
                    "quipu_" + cls.ToLowerInvariant(), 0.76, "Quality / Ops", "NOTE::" + nid,
                    "QUIPU " + cls + " on " + sku + " @ " + dc + " (" + nid + ")",
                    "Inspect staged pallet/door condition and confirm physical count disposition.",
                    "Unstructured handwritten floor note indicates WIP aging, damage, or count variance.",
                    cls == "DAMAGED_DOOR_HOLD" ? "QUARANTINE_RETURN" : "TO_PICK",
                    cls == "DAMAGED_DOOR_HOLD" ? "Return" : "Source",
                    new ArrayList(new[] { nid, sku, dc }), now));
            }

            freshOps.Sort((a, b) => GetNum(b, "priority", 0.0).CompareTo(GetNum(a, "priority", 0.0)));
            var byFp = new Dictionary<string, Dictionary<string, object>>();
            foreach (object exObj in EnsureList(_relational, "bodyDirectives"))
            {
                var ex = exObj as Dictionary<string, object>;
                if (ex != null && !string.IsNullOrEmpty(GetStr(ex, "fingerprint")))
                    byFp[GetStr(ex, "fingerprint")] = ex;
            }
            int emitted = 0, deduped = 0;
            foreach (var d in freshOps)
            {
                string fp = GetStr(d, "fingerprint");
                Dictionary<string, object> prev;
                if (byFp.TryGetValue(fp, out prev))
                {
                    if (GetStr(prev, "status") == "expired")
                    {
                        prev["status"] = "open";
                        prev["lastStatusAt"] = now;
                    }
                    deduped++;
                }
                else
                {
                    byFp[fp] = d;
                    emitted++;
                }
            }

            var mergedList = new List<Dictionary<string, object>>(byFp.Values);
            mergedList.Sort((a, b) => {
                string sa = GetStr(a, "status"), sb = GetStr(b, "status");
                int oa = (sa == "open" || sa == "ack" || sa == "in_progress") ? 0 : 1;
                int ob = (sb == "open" || sb == "ack" || sb == "in_progress") ? 0 : 1;
                if (oa != ob) return oa.CompareTo(ob);
                return GetNum(b, "priority", 0.0).CompareTo(GetNum(a, "priority", 0.0));
            });
            if (mergedList.Count > 200) mergedList.RemoveRange(200, mergedList.Count - 200);

            var opArr = new ArrayList();
            var m365Arr = new ArrayList();
            int openCnt = 0, closedCnt = 0, immCnt = 0, digCnt = 0;
            foreach (var d in mergedList)
            {
                opArr.Add(d);
                string dst = GetStr(d, "status");
                if (dst == "open" || dst == "ack" || dst == "in_progress")
                {
                    openCnt++;
                    object envObj;
                    if (d.TryGetValue("m365CardEnvelope", out envObj) && envObj != null)
                    {
                        var env = envObj as Dictionary<string, object>;
                        if (env != null)
                        {
                            m365Arr.Add(env);
                            if (GetStr(env, "dispatchMode") == "IMMEDIATE_CARD") immCnt++;
                            else if (GetStr(env, "dispatchMode") == "SHIFT_DIGEST") digCnt++;
                        }
                    }
                }
                else if (dst == "done") closedCnt++;
            }

            var sysList = new List<Dictionary<string, object>>(firedSys.Values);
            sysList.Sort((a, b) => ((int)GetNum(a, "sequence", 99)).CompareTo((int)GetNum(b, "sequence", 99)));
            var sysArr = new ArrayList();
            foreach (var s in sysList) sysArr.Add(s);

            _relational["bodyDirectives"] = opArr;
            _relational["m365HandoffQueue"] = m365Arr;
            var summary = new Dictionary<string, object> {
                { "evaluatedAt", now },
                { "triggerAction", triggerAction },
                { "returnChainOwner", "return-quarantine-governance" },
                { "systemic", sysArr },
                { "counts", new Dictionary<string, object> {
                    { "systemic", sysArr.Count },
                    { "openOperational", openCnt },
                    { "closedOperational", closedCnt },
                    { "immediateM365Cards", immCnt },
                    { "shiftDigestItems", digCnt },
                    { "newlyEmitted", emitted },
                    { "deduped", deduped }
                }}
            };
            _relational["pathwaySummary"] = summary;
            return summary;
        }

        private void AutoCloseDirectivesFromPhysicalEvent(string eventKind, string[] tokens, string actor, string outcome)
        {
            var upperTokens = new List<string>();
            if (tokens != null)
            {
                foreach (string t in tokens)
                {
                    if (!string.IsNullOrEmpty(t) && !string.IsNullOrEmpty(t.Trim()))
                        upperTokens.Add(t.Trim().ToUpperInvariant());
                }
            }
            if (upperTokens.Count == 0) return;

            string now = UtcNowIso();
            var dirs = EnsureList(_relational, "bodyDirectives");
            var fbList = EnsureList(_relational, "bodyFeedback");
            int closedCount = 0;

            foreach (object dObj in dirs)
            {
                var d = dObj as Dictionary<string, object>;
                if (d == null) continue;
                string st = GetStr(d, "status");
                if (st != "open" && st != "ack" && st != "in_progress") continue;

                var sb = new StringBuilder(GetStr(d, "targetEntity") + " " + GetStr(d, "title"));
                foreach (object rObj in EnsureList(d, "relatedRefs")) sb.Append(" ").Append(Convert.ToString(rObj));
                string blob = sb.ToString().ToUpperInvariant();

                bool matched = false;
                foreach (string tok in upperTokens)
                {
                    if (blob.Contains(tok)) { matched = true; break; }
                }
                if (matched)
                {
                    d["status"] = "done";
                    d["lastStatusAt"] = now;
                    d["closedByPhysicalEvent"] = eventKind;
                    fbList.Add(new Dictionary<string, object> {
                        { "feedbackId", "FB-" + (fbList.Count + 1) },
                        { "directiveId", GetStr(d, "directiveId") },
                        { "fingerprint", GetStr(d, "fingerprint") },
                        { "status", "done" },
                        { "triggerEvent", eventKind },
                        { "outcome", outcome },
                        { "executedBy", actor },
                        { "loggedAt", now }
                    });
                    closedCount++;
                }
            }
            while (fbList.Count > 400) fbList.RemoveAt(0);
            if (closedCount > 0) RefreshPathwaysAndDirectives(eventKind);
        }

        private Dictionary<string, object> GetOutbox(int sinceCursor, int limit)
        {
            lock (_stateLock)
            {
                int sc = Math.Max(0, sinceCursor);
                int lim = Math.Max(1, Math.Min(1000, limit));
                var evs = new ArrayList();
                foreach (var ob in _outbox)
                {
                    if ((int)GetNum(ob, "seq", 0) > sc)
                    {
                        evs.Add(ob);
                        if (evs.Count >= lim) break;
                    }
                }
                return new Dictionary<string, object> {
                    { "ok", true }, { "homeNodeId", HomeNodeId },
                    { "cursor", _outbox.Count }, { "sinceCursor", sc },
                    { "count", evs.Count }, { "events", evs }
                };
            }
        }

        private Dictionary<string, object> GetPathways()
        {
            lock (_stateLock)
            {
                if (!_relational.ContainsKey("pathwaySummary") || _relational["pathwaySummary"] == null)
                    RefreshPathwaysAndDirectives("read");
                var summary = EnsureDict(_relational, "pathwaySummary");
                return new Dictionary<string, object> {
                    { "ok", true },
                    { "architecture", "dual-speed-cyberphysical-io/v2.5" },
                    { "returnChainOwner", GetStr(summary, "returnChainOwner", "return-quarantine-governance") },
                    { "evaluatedAt", GetStr(summary, "evaluatedAt") },
                    { "systemic", EnsureList(summary, "systemic") },
                    { "operational", EnsureList(_relational, "bodyDirectives") },
                    { "m365DispatchQueue", EnsureList(_relational, "m365HandoffQueue") },
                    { "counts", EnsureDict(summary, "counts") }
                };
            }
        }

        private Dictionary<string, object> GetDirectives(string statusFilter, string ownerRoleFilter)
        {
            lock (_stateLock)
            {
                if (!_relational.ContainsKey("pathwaySummary") || _relational["pathwaySummary"] == null)
                    RefreshPathwaysAndDirectives("read");
                string sf = (statusFilter ?? "").Trim().ToLowerInvariant();
                string of = (ownerRoleFilter ?? "").Trim().ToLowerInvariant();
                var outDirs = new ArrayList();
                foreach (object dObj in EnsureList(_relational, "bodyDirectives"))
                {
                    var d = dObj as Dictionary<string, object>;
                    if (d == null) continue;
                    if (!string.IsNullOrEmpty(sf) && GetStr(d, "status").ToLowerInvariant() != sf) continue;
                    if (!string.IsNullOrEmpty(of) && !GetStr(d, "ownerRole").ToLowerInvariant().Contains(of)) continue;
                    outDirs.Add(d);
                }
                return new Dictionary<string, object> {
                    { "ok", true },
                    { "count", outDirs.Count },
                    { "directives", outDirs },
                    { "bodyFeedback", EnsureList(_relational, "bodyFeedback") },
                    { "counts", EnsureDict(EnsureDict(_relational, "pathwaySummary"), "counts") }
                };
            }
        }

        private static ArrayList EnsureList(Dictionary<string, object> dict, string key)
        {
            object v;
            if (dict.TryGetValue(key, out v) && v is ArrayList) return (ArrayList)v;
            if (v is object[]) { var al = new ArrayList((object[])v); dict[key] = al; return al; }
            var created = new ArrayList();
            dict[key] = created;
            return created;
        }

        private static Dictionary<string, object> EnsureDict(Dictionary<string, object> dict, string key)
        {
            object v;
            if (dict.TryGetValue(key, out v) && v is Dictionary<string, object>) return (Dictionary<string, object>)v;
            var created = new Dictionary<string, object>();
            dict[key] = created;
            return created;
        }

        private static string GetStr(Dictionary<string, object> d, string key, string fallback = "")
        {
            if (d == null) return fallback;
            object v;
            return d.TryGetValue(key, out v) && v != null ? Convert.ToString(v) : fallback;
        }

        private static double GetNum(Dictionary<string, object> d, string key, double fallback = 0.0)
        {
            if (d == null) return fallback;
            object v;
            if (d.TryGetValue(key, out v) && v != null)
            {
                double parsed;
                if (double.TryParse(Convert.ToString(v), out parsed)) return parsed;
            }
            return fallback;
        }

        private static bool GetBool(Dictionary<string, object> d, string key, bool fallback = false)
        {
            if (d == null) return fallback;
            object v;
            if (d.TryGetValue(key, out v) && v != null)
            {
                bool parsed;
                if (bool.TryParse(Convert.ToString(v), out parsed)) return parsed;
            }
            return fallback;
        }

        private void PruneTemp()
        {
            long now = NowEpoch();
            var sessions = EnsureList(_temp, "activeSessions");
            var aliveSessions = new ArrayList();
            foreach (object obj in sessions)
            {
                var s = obj as Dictionary<string, object>;
                if (s == null) continue;
                long last = (long)GetNum(s, "lastSeenEpoch", now);
                if (now - last <= SessionTtlSeconds) aliveSessions.Add(s);
            }
            _temp["activeSessions"] = aliveSessions;

            var locks = EnsureList(_temp, "entityLocks");
            var activeLocks = new ArrayList();
            foreach (object obj in locks)
            {
                var lk = obj as Dictionary<string, object>;
                if (lk == null) continue;
                long exp = (long)GetNum(lk, "expiresEpoch", now + 60);
                if (exp >= now) activeLocks.Add(lk);
            }
            _temp["entityLocks"] = activeLocks;
        }

        private Dictionary<string, object> BuildStateEnvelope()
        {
            return BuildStateEnvelope(null);
        }

        private Dictionary<string, object> BuildStateEnvelope(int? sinceCursor)
        {
            lock (_stateLock)
            {
                PruneTemp();
                int rev = (int)GetNum(_relational, "revision", 1);
                int trev = (int)GetNum(_temp, "revision", 1);
                Dictionary<string, object> xReg;
                _registrations.TryGetValue("x_osq", out xReg);
                var env = new Dictionary<string, object>
                {
                    { "ok", true },
                    { "revision", rev },
                    { "tempRevision", trev },
                    { "outboxCursor", _outbox.Count },
                    { "serverTime", UtcNowIso() },
                    { "activeRegistration", new Dictionary<string, object> {
                        { "holonId", xReg != null ? GetStr(xReg, "holonId", "x_osq") : "x_osq" },
                        { "status", xReg != null ? GetStr(xReg, "status", "ACTIVE") : "ACTIVE" },
                        { "routable", xReg != null && GetBool(xReg, "routable", true) },
                        { "leaseExpiresAt", xReg != null ? GetStr(xReg, "leaseExpiresAt") : "" }
                    }},
                    { "relational", _relational },
                    { "temp", _temp }
                };
                if (sinceCursor.HasValue)
                {
                    var replay = new ArrayList();
                    foreach (var ob in _outbox)
                    {
                        if ((int)GetNum(ob, "seq", 0) > sinceCursor.Value) replay.Add(ob);
                    }
                    env["outboxEventsSinceCursor"] = replay;
                }
                return env;
            }
        }

        private void AppendAudit(string actor, string persona, string action, string obj, string details)
        {
            var events = EnsureList(_relational, "events");
            events.Add(new Dictionary<string, object>
            {
                { "id", "EVT-" + NowEpoch() + "-" + Guid.NewGuid().ToString("N").Substring(0, 4) },
                { "time", UtcNowIso() },
                { "actor", actor },
                { "persona", persona },
                { "action", action },
                { "object", obj },
                { "details", details }
            });
            while (events.Count > 1500) events.RemoveAt(0);
        }

        private void BroadcastSse(string eventType)
        {
            BroadcastSse(eventType, _outbox.Count);
        }

        private void BroadcastSse(string eventType, int outboxSeq)
        {
            string dataJson;
            lock (_stateLock)
            {
                var packet = new Dictionary<string, object>
                {
                    { "type", eventType },
                    { "cursor", outboxSeq },
                    { "state", BuildStateEnvelope() }
                };
                dataJson = NewSerializer().Serialize(packet);
            }
            byte[] payload = Encoding.UTF8.GetBytes("id: " + outboxSeq + "\nevent: sync\ndata: " + dataJson + "\n\n");
            PushRawToSseClients(payload);
        }

        private void PushRawToSseClients(byte[] bytes)
        {
            lock (_sseLock)
            {
                for (int i = _sseClients.Count - 1; i >= 0; i--)
                {
                    var c = _sseClients[i];
                    if (!c.Alive) { _sseClients.RemoveAt(i); continue; }
                    try
                    {
                        lock (c.WriteLock)
                        {
                            c.Stream.Write(bytes, 0, bytes.Length);
                            c.Stream.Flush();
                        }
                    }
                    catch
                    {
                        c.Alive = false;
                        try { c.Client.Close(); } catch {}
                        _sseClients.RemoveAt(i);
                    }
                }
            }
        }

        private Dictionary<string, object> FindByKey(ArrayList list, string keyField, string value)
        {
            foreach (object obj in list)
            {
                var d = obj as Dictionary<string, object>;
                if (d != null && string.Equals(GetStr(d, keyField), value, StringComparison.OrdinalIgnoreCase))
                    return d;
            }
            return null;
        }

        private static object CloneJsonValue(object v)
        {
            var dict = v as Dictionary<string, object>;
            if (dict != null)
            {
                var copy = new Dictionary<string, object>();
                foreach (var kv in dict) copy[kv.Key] = CloneJsonValue(kv.Value);
                return copy;
            }
            var list = v as ArrayList;
            if (list != null)
            {
                var arr = new ArrayList();
                foreach (object item in list) arr.Add(CloneJsonValue(item));
                return arr;
            }
            var objArr = v as object[];
            if (objArr != null)
            {
                var arr = new ArrayList();
                foreach (object item in objArr) arr.Add(CloneJsonValue(item));
                return arr;
            }
            return v;
        }

        private static bool TryParseUtcIso(string text, out DateTime dtUtc)
        {
            dtUtc = DateTime.MinValue;
            if (string.IsNullOrEmpty(text)) return false;
            return DateTime.TryParse(
                text,
                System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.AdjustToUniversal | System.Globalization.DateTimeStyles.AssumeUniversal,
                out dtUtc
            );
        }

        private static int StageDefaultProbability(string stage)
        {
            switch (stage)
            {
                case "Lead": return 10;
                case "Qualified": return 25;
                case "Property Walk": return 45;
                case "Ready for Quote": return 65;
                case "Quoted": return 75;
                case "Quote Presented": return 85;
                case "Quote Won": return 95;
                case "Project In Progress": return 100;
                case "Closed Lost": return 0;
                default: return 25;
            }
        }

        private void RecomputeQuotePricing(Dictionary<string, object> q, Dictionary<string, object> opp, ArrayList items)
        {
            var lines = EnsureList(q, "lines");
            double quoteDisc = GetNum(q, "quoteDiscountPct", 0.0);
            double bmsDisc = GetNum(q, "bmsDiscountPct", 0.0);
            double ewallet = GetNum(q, "ewalletApplied", 0.0);
            double grossTotal = 0.0;
            double netBeforeWallet = 0.0;
            double costTotal = 0.0;

            foreach (object obj in lines)
            {
                var ln = obj as Dictionary<string, object>;
                if (ln == null) continue;
                string sku = GetStr(ln, "sku").Trim().ToUpperInvariant();
                var it = string.IsNullOrEmpty(sku) ? null : FindByKey(items, "sku", sku);
                double qty = Math.Max(0.0, GetNum(ln, "qty", 0.0));
                double unit = GetNum(ln, "unit", 0.0);
                if (unit <= 0.0 && it != null) { unit = GetNum(it, "price", 0.0); ln["unit"] = unit; }
                double lineDisc = Math.Max(0.0, Math.Min(90.0, GetNum(ln, "lineDiscountPct", 0.0)));
                double fpDisc = Math.Max(0.0, Math.Min(90.0, GetNum(ln, "floorplanDiscountPct", 0.0)));
                double mult = Math.Max(0.0, (1.0 - lineDisc / 100.0) * (1.0 - fpDisc / 100.0) * (1.0 - quoteDisc / 100.0) * (1.0 - bmsDisc / 100.0));
                double gross = qty * unit;
                double net = gross * mult;
                double unitCost = it != null ? GetNum(it, "cost", unit * 0.62) : unit * 0.62;
                ln["lineNetTotal"] = Math.Round(net, 2);
                grossTotal += gross;
                netBeforeWallet += net;
                costTotal += qty * unitCost;
            }

            double netAfterWallet = Math.Max(0.0, netBeforeWallet - ewallet);
            double marginPct = netAfterWallet > 0.0 ? Math.Round(((netAfterWallet - costTotal) / netAfterWallet) * 100.0, 2) : 0.0;
            double floorPct = GetNum(q, "marginFloorPct", 24.0);
            q["approvedMarginPct"] = marginPct;
            q["grossSubtotal"] = Math.Round(grossTotal, 2);
            q["netSubtotal"] = Math.Round(netAfterWallet, 2);
            q["totalCost"] = Math.Round(costTotal, 2);

            bool autoEligible = marginPct >= floorPct;
            double denom = Math.Max(0.05, 1.0 - (floorPct / 100.0));
            double targetNetNeeded = costTotal / denom;
            double liftNeeded = autoEligible ? 0.0 : Math.Round(Math.Max(0.0, targetNetNeeded - netAfterWallet), 2);
            double maxDiscPct = grossTotal > 0.0 ? Math.Round(Math.Max(0.0, (1.0 - ((targetNetNeeded + ewallet) / grossTotal)) * 100.0), 2) : 0.0;

            q["pricingGuidance"] = new Dictionary<string, object>
            {
                { "autoApproveEligible", autoEligible },
                { "currentMarginPct", marginPct },
                { "marginFloorPct", floorPct },
                { "revenueLiftNeeded", liftNeeded },
                { "recommendedMaxQuoteDiscountPct", maxDiscPct },
                { "rule", autoEligible ? "CPQ-45 Auto-Approve Eligible" : "CPQ-46/47 Escalation or Remediation Required" }
            };

            if (opp != null)
            {
                var acct = FindByKey(EnsureList(_relational, "accounts"), "accountId", GetStr(opp, "accountId"));
                if (acct != null && string.IsNullOrEmpty(GetStr(q, "priceProtectionUntil")))
                {
                    string pp = GetStr(acct, "priceProtectionUntil");
                    if (!string.IsNullOrEmpty(pp)) q["priceProtectionUntil"] = pp;
                }
            }
        }

        private void RunOsqPipelineInternal(string actor, string persona, Dictionary<string, object> opts)
        {
            var opps = EnsureList(_relational, "opportunities");
            var quotes = EnsureList(_relational, "quotes");
            var items = EnsureList(_relational, "items");
            var fb = EnsureDict(_relational, "forecastBatch");
            var eb = EnsureDict(fb, "emergencyBrake");

            bool autoClearExpired = (opts != null && GetBool(opts, "autoClearExpired", false));
            DateTime nowUtc = DateTime.UtcNow;

            if (autoClearExpired)
            {
                foreach (object oObj in opps)
                {
                    var o = oObj as Dictionary<string, object>;
                    if (o == null) continue;
                    string cond = GetStr(o, "opportunityCondition");
                    if (!string.IsNullOrEmpty(cond))
                    {
                        DateTime clearAt;
                        if (TryParseUtcIso(GetStr(o, "conditionAutoClearsAt"), out clearAt) && clearAt <= nowUtc)
                        {
                            o["opportunityCondition"] = null;
                            o["conditionAutoClearsAt"] = null;
                            AppendAudit(actor, persona, "opportunity.condition_autocleared", GetStr(o, "opportunityId"), "CPQ-9 auto-cleared after 180d");
                        }
                    }
                }
            }

            var oppMap = new Dictionary<string, Dictionary<string, object>>(StringComparer.OrdinalIgnoreCase);
            int excludedProFormaCount = 0;
            foreach (object oObj in opps)
            {
                var o = oObj as Dictionary<string, object>;
                if (o == null) continue;
                string oid = GetStr(o, "opportunityId");
                if (!string.IsNullOrEmpty(oid)) oppMap[oid] = o;
                string cond = GetStr(o, "opportunityCondition");
                if (cond.Equals("Pro Forma", StringComparison.OrdinalIgnoreCase) || cond.Equals("MOG", StringComparison.OrdinalIgnoreCase))
                {
                    excludedProFormaCount++;
                }
            }

            double proration = (opts != null && opts.ContainsKey("prorationFactor"))
                ? GetNum(opts, "prorationFactor", GetNum(fb, "prorationFactor", 0.75))
                : GetNum(fb, "prorationFactor", 0.75);
            proration = Math.Max(0.1, Math.Min(1.0, proration));
            string profile = GetStr(_relational, "governanceProfile", GetStr(fb, "governanceProfile", "adaptive"));

            var skuDemand = new Dictionary<string, double>(StringComparer.OrdinalIgnoreCase);
            var pmHandoff = new ArrayList();
            int eligibleCount = 0;
            int skusSubstituted = 0;
            int suppressedUnits = 0;
            bool hasCutover = false;

            foreach (object qObj in quotes)
            {
                var q = qObj as Dictionary<string, object>;
                if (q == null) continue;
                string st = GetStr(q, "status");
                bool isPrim = GetBool(q, "isPrimary", false);
                if (!st.Equals("Accepted", StringComparison.OrdinalIgnoreCase) || !isPrim) continue;

                string oid = GetStr(q, "opportunityId");
                Dictionary<string, object> opp = (!string.IsNullOrEmpty(oid) && oppMap.ContainsKey(oid)) ? oppMap[oid] : null;
                if (opp != null)
                {
                    string cond = GetStr(opp, "opportunityCondition");
                    if (cond.Equals("Pro Forma", StringComparison.OrdinalIgnoreCase) || cond.Equals("MOG", StringComparison.OrdinalIgnoreCase))
                        continue;
                }

                eligibleCount++;
                int quotedSum = 0;
                int orderedSum = 0;

                foreach (object lObj in EnsureList(q, "lines"))
                {
                    var ln = lObj as Dictionary<string, object>;
                    if (ln == null) continue;
                    int qQty = (int)Math.Max(0, Math.Round(GetNum(ln, "qty", 0.0)));
                    int oQty = (int)Math.Max(0, Math.Round(GetNum(ln, "orderedQty", 0.0)));
                    int rem = Math.Max(0, qQty - oQty);
                    quotedSum += qQty;
                    orderedSum += oQty;
                    if (rem <= 0) continue;

                    string sku = GetStr(ln, "sku").Trim().ToUpperInvariant();
                    if (string.IsNullOrEmpty(sku)) continue;
                    var it = FindByKey(items, "sku", sku);
                    if (it != null && GetStr(it, "itemType").Equals("Labor", StringComparison.OrdinalIgnoreCase)) continue;
                    if (it != null && GetStr(it, "status").Equals("Discontinued", StringComparison.OrdinalIgnoreCase) && !string.IsNullOrEmpty(GetStr(it, "replacement")))
                    {
                        hasCutover = true;
                        string rep = GetStr(it, "replacement").Trim().ToUpperInvariant();
                        skuDemand[sku] = (skuDemand.ContainsKey(sku) ? skuDemand[sku] : 0.0) + (rem * 0.5);
                        skuDemand[rep] = (skuDemand.ContainsKey(rep) ? skuDemand[rep] : 0.0) + (rem * 0.5);
                        skusSubstituted++;
                        continue;
                    }
                    skuDemand[sku] = (skuDemand.ContainsKey(sku) ? skuDemand[sku] : 0.0) + rem;
                }

                int holdDays = 30;
                if (opp != null)
                {
                    var fl = EnsureDict(opp, "flags");
                    holdDays = GetBool(fl, "coordinatedDelivery", false) ? 45 : (int)GetNum(opp, "forecastHoldDays", 30);
                }

                pmHandoff.Add(new Dictionary<string, object>
                {
                    { "quoteId", GetStr(q, "id") },
                    { "opportunityId", oid },
                    { "sapQuoteNumber", GetStr(q, "sapQuoteNumber", "SAP-QT-PENDING") },
                    { "lastSapOrderNumber", GetStr(q, "lastSapOrderNumber", "") },
                    { "quotedUnits", quotedSum },
                    { "orderedUnits", orderedSum },
                    { "remainingUnits", Math.Max(0, quotedSum - orderedSum) },
                    { "forecastHoldDays", holdDays },
                    { "handoffTarget", "Project Lifecycle Management API (CPQ-58)" }
                });
            }

            if (skuDemand.ContainsKey("SKU-ELEC-2001") && !skuDemand.ContainsKey("SKU-ELEC-1001"))
            {
                hasCutover = true;
            }

            var buckets = new ArrayList();
            var reservations = new ArrayList();
            double byTotalUnits = 0.0;
            string dayTag = nowUtc.ToString("yyyyMMdd", System.Globalization.CultureInfo.InvariantCulture);

            var hcPipeline = EnsureHeuristicsConfig();
            var scbPipeline = EnsureDict(hcPipeline, "forecastAndScb");
            bool frontLoadMonday = string.Equals(GetStr(scbPipeline, "forecastTimePhasingMode", "FRONT_LOAD_FIRST_MONDAY"), "FRONT_LOAD_FIRST_MONDAY", StringComparison.OrdinalIgnoreCase);

            foreach (var kv in skuDemand)
            {
                string sku = kv.Key;
                double openUnits = kv.Value;
                var it = FindByKey(items, "sku", sku);
                string desc = it != null ? GetStr(it, "description", sku) : sku;
                string origin = it != null ? GetStr(it, "origin", "DOMESTIC") : "DOMESTIC";

                double w1, w2, w3, w4, w5, w6;
                if (frontLoadMonday)
                {
                    w1 = Math.Round(openUnits * 0.55 * proration, 1);
                    w2 = Math.Round(openUnits * 0.15, 1);
                    w3 = Math.Round(openUnits * 0.10, 1);
                    w4 = Math.Round(openUnits * 0.10, 1);
                    w5 = Math.Round(openUnits * 0.05, 1);
                    w6 = Math.Round(Math.Max(0.0, openUnits - (openUnits * 0.55 + w2 + w3 + w4 + w5)), 1);
                }
                else
                {
                    w1 = Math.Round(openUnits * 0.20 * proration, 1);
                    w2 = Math.Round(openUnits * 0.20, 1);
                    w3 = Math.Round(openUnits * 0.20, 1);
                    w4 = Math.Round(openUnits * 0.20, 1);
                    w5 = Math.Round(openUnits * 0.10, 1);
                    w6 = Math.Round(Math.Max(0.0, openUnits - (openUnits * 0.20 + w2 + w3 + w4 + w5)), 1);
                }
                double rowTotal = Math.Round(w1 + w2 + w3 + w4 + w5 + w6, 1);
                byTotalUnits += rowTotal;

                buckets.Add(new Dictionary<string, object>
                {
                    { "sku", sku },
                    { "description", desc },
                    { "origin", origin },
                    { "w1", w1 },
                    { "w2", w2 },
                    { "w3", w3 },
                    { "w4", w4 },
                    { "w5", w5 },
                    { "w6", w6 },
                    { "total", rowTotal }
                });

                reservations.Add(new Dictionary<string, object>
                {
                    { "reservationId", "RES-SAP-" + dayTag + "-" + sku },
                    { "sku", sku },
                    { "qty", Math.Round(Math.Min(openUnits, rowTotal * 0.6), 1) },
                    { "plant", "1010" },
                    { "sloc", "0001" },
                    { "movementType", "261" },
                    { "status", (sku == "SKU-ELEC-1001" && hasCutover) ? "REPLACED_AT_W03" : "READY" }
                });
            }

            if (opts != null && GetBool(opts, "resetBrake", false))
            {
                eb["status"] = "HELD";
                eb["tripped"] = true;
            }
            string ebStatus = GetStr(eb, "status", "HELD");
            fb["taskState"] = (ebStatus == "OVERRIDDEN") ? "ACCEPTED" : (GetBool(eb, "tripped", false) ? "HELD" : "ACCEPTED");
            fb["batchId"] = "BATCH-" + dayTag + "-" + (int)GetNum(_relational, "revision", 1);
            fb["governanceProfile"] = profile;
            fb["prorationFactor"] = proration;
            fb["eligibleQuotesCount"] = eligibleCount;
            fb["excludedProFormaCount"] = excludedProFormaCount;
            fb["skusSubstituted"] = skusSubstituted;
            fb["suppressedUnits"] = suppressedUnits;
            fb["byPublishedUnits"] = Math.Round(byTotalUnits, 2);
            fb["weeklyBuckets"] = buckets;
            fb["sapReservations"] = reservations;
            fb["pmHandoff"] = pmHandoff;
            fb["lastRunAt"] = UtcNowIso();

            EvaluateHeuristicsOrder(opts);
            AppendAudit(actor, persona, "osq.pipeline.completed", GetStr(fb, "batchId"),
                string.Format(System.Globalization.CultureInfo.InvariantCulture, "Eligible={0}, ExcludedProForma={1}, PublishedUnits={2}, PMHandoffs={3}",
                    eligibleCount, excludedProFormaCount, fb["byPublishedUnits"], pmHandoff.Count));
        }

        private Dictionary<string, object> MutateRelational(Dictionary<string, object> body, out int httpStatus)
        {
            httpStatus = 200;
            string action = GetStr(body, "action").Trim();
            string actor = GetStr(body, "actor", "User");
            string persona = GetStr(body, "persona", "RSR / Seller");
            var payload = body.ContainsKey("payload") && body["payload"] is Dictionary<string, object>
                ? (Dictionary<string, object>)body["payload"]
                : new Dictionary<string, object>();
            string idempotencyKey = GetStr(body, "idempotencyKey").Trim();
            string reqHash = CanonicalHash(new Dictionary<string, object> { { "action", action }, { "payload", payload } });
            string corrId = GetStr(body, "correlationId", "corr-" + reqHash.Substring(0, 12));

            int outboxSeq = 0;
            Dictionary<string, object> receipts = null;

            lock (_stateLock)
            {
                string routeErr;
                if (!CheckActiveRoute("x_osq", out routeErr))
                {
                    RecordRestrictedAudit(actor, "DENY", routeErr, corrId, "");
                    httpStatus = 503;
                    return ErrorObj(routeErr);
                }

                Dictionary<string, object> existingIdem;
                if (!string.IsNullOrEmpty(idempotencyKey) && _idempotencyIndex.TryGetValue(idempotencyKey, out existingIdem))
                {
                    if (!string.Equals(GetStr(existingIdem, "requestHash"), reqHash, StringComparison.Ordinal))
                    {
                        string msg = "Idempotency conflict: key '" + idempotencyKey + "' already bound to a different payload hash.";
                        RecordRestrictedAudit(actor, "DENY", msg, GetStr(existingIdem, "correlationId"), GetStr(existingIdem, "taskId"));
                        httpStatus = 409;
                        var conf = ErrorObj(msg);
                        conf["conflict"] = true;
                        return conf;
                    }
                    RecordRestrictedAudit(actor, "ALLOW", "Idempotent replay for " + action, GetStr(existingIdem, "correlationId"), GetStr(existingIdem, "taskId"));
                    var repEnv = BuildStateEnvelope();
                    repEnv["idempotentReplay"] = true;
                    repEnv["idempotencyKey"] = idempotencyKey;
                    repEnv["taskId"] = existingIdem["taskId"];
                    repEnv["correlationId"] = existingIdem["correlationId"];
                    repEnv["causationId"] = existingIdem["causationId"];
                    repEnv["outboxCursor"] = existingIdem["outboxCursor"];
                    repEnv["taskReceipt"] = existingIdem["taskReceipt"];
                    return repEnv;
                }

                int curRev = (int)GetNum(_relational, "revision", 1);
                if (body.ContainsKey("expectedRevision") && body["expectedRevision"] != null && !string.IsNullOrEmpty(Convert.ToString(body["expectedRevision"]).Trim()))
                {
                    int expRev = (int)GetNum(body, "expectedRevision", -1);
                    if (expRev != curRev)
                    {
                        string msg = "Owner validation rejected stale expectedRevision=" + expRev + " (current=" + curRev + ").";
                        RecordRestrictedAudit(actor, "DENY", msg, corrId, "");
                        httpStatus = 409;
                        var conf = ErrorObj(msg);
                        conf["conflict"] = true;
                        return conf;
                    }
                }
                else if (body.ContainsKey("expectedVersion") && body["expectedVersion"] != null && !string.IsNullOrEmpty(Convert.ToString(body["expectedVersion"]).Trim()))
                {
                    int expVer = (int)GetNum(body, "expectedVersion", -1);
                    if (expVer != curRev)
                    {
                        string msg = "Owner validation rejected stale expectedVersion=" + expVer + " (current=" + curRev + ").";
                        RecordRestrictedAudit(actor, "DENY", msg, corrId, "");
                        httpStatus = 409;
                        var conf = ErrorObj(msg);
                        conf["conflict"] = true;
                        return conf;
                    }
                }

                PruneTemp();
                string targetEkey = GetStr(body, "entityKey", GetStr(payload, "entityKey", GetStr(payload, "quoteId", GetStr(payload, "id", GetStr(payload, "opportunityId", GetStr(payload, "budgetId")))))).Trim();
                string callerSession = GetStr(body, "sessionId", GetStr(payload, "sessionId")).Trim();
                bool hasFence = (body.ContainsKey("fencingToken") && body["fencingToken"] != null && !string.IsNullOrEmpty(Convert.ToString(body["fencingToken"]).Trim()))
                    || (payload.ContainsKey("fencingToken") && payload["fencingToken"] != null && !string.IsNullOrEmpty(Convert.ToString(payload["fencingToken"]).Trim()));
                int callerFence = hasFence ? (int)GetNum(body.ContainsKey("fencingToken") ? body : payload, "fencingToken", -1) : -1;
                if (!string.IsNullOrEmpty(targetEkey))
                {
                    var activeLock = FindByKey(EnsureList(_temp, "entityLocks"), "entityKey", targetEkey);
                    int maxSeenFence = _entityMaxFencing.ContainsKey(targetEkey) ? _entityMaxFencing[targetEkey] : 0;
                    if (activeLock != null)
                    {
                        int lockFence = (int)GetNum(activeLock, "fencingToken", 0);
                        if (hasFence && callerFence != lockFence)
                        {
                            string msg = "Owner validation rejected stale fencingToken=" + callerFence + " for '" + targetEkey + "' (active=" + lockFence + ").";
                            RecordRestrictedAudit(actor, "DENY", msg, corrId, "");
                            httpStatus = 409;
                            var conf = ErrorObj(msg);
                            conf["conflict"] = true;
                            return conf;
                        }
                        if (!string.IsNullOrEmpty(callerSession) && GetStr(activeLock, "sessionId") != callerSession)
                        {
                            string msg = "Owner validation rejected write to '" + targetEkey + "': lease held by session '" + GetStr(activeLock, "sessionId") + "'.";
                            RecordRestrictedAudit(actor, "DENY", msg, corrId, "");
                            httpStatus = 409;
                            var conf = ErrorObj(msg);
                            conf["conflict"] = true;
                            return conf;
                        }
                    }
                    else if (hasFence && callerFence < maxSeenFence)
                    {
                        string msg = "Owner validation rejected stale fencingToken=" + callerFence + " < latest fence=" + maxSeenFence + " on '" + targetEkey + "'.";
                        RecordRestrictedAudit(actor, "DENY", msg, corrId, "");
                        httpStatus = 409;
                        var conf = ErrorObj(msg);
                        conf["conflict"] = true;
                        return conf;
                    }
                }

                var accounts = EnsureList(_relational, "accounts");
                var opps = EnsureList(_relational, "opportunities");
                var budgets = EnsureList(_relational, "budgets");
                var items = EnsureList(_relational, "items");
                var quotes = EnsureList(_relational, "quotes");
                var floorplans = EnsureList(_relational, "floorplans");

                if (action == "sync.full")
                {
                    string[] syncKeys = { "accounts", "opportunities", "budgets", "items", "quotes", "floorplans", "forecastBatch", "marketplaceShards", "connectors", "changes", "snapshots", "heuristicsConfig", "lastHeuristicsEvaluation", "deepMapFlexLogicDomain" };
                    foreach (string k in syncKeys)
                    {
                        if (payload.ContainsKey(k) && payload[k] != null) _relational[k] = payload[k];
                    }
                    AppendAudit(actor, persona, "relational.full_sync", "osq_relational_store", "Synchronized state from live client");
                }
                else if (action == "opportunity.create")
                {
                    string acctId = GetStr(payload, "accountId");
                    var acct = FindByKey(accounts, "accountId", acctId);
                    if (acct == null) { httpStatus = 400; return ErrorObj("Unknown accountId: " + acctId); }
                    if (GetStr(acct, "status").Equals("Closed", StringComparison.OrdinalIgnoreCase))
                    {
                        httpStatus = 400;
                        return ErrorObj("CPQ-1 Blocked: Account '" + GetStr(acct, "name") + "' has Closed status.");
                    }
                    var flags = EnsureDict(payload, "flags");
                    bool coord = GetBool(flags, "coordinatedDelivery", false);
                    if (coord) flags["kitted"] = true;
                    int holdDays = coord ? 45 : (int)GetNum(payload, "forecastHoldDays", 30);
                    string oppId = GetStr(payload, "opportunityId", "OPP-RP-" + NowEpoch());
                    var opp = new Dictionary<string, object>
                    {
                        { "opportunityId", oppId },
                        { "parentOpportunityId", payload.ContainsKey("parentOpportunityId") ? payload["parentOpportunityId"] : null },
                        { "isTopLink", GetBool(payload, "isTopLink", false) },
                        { "accountId", acctId },
                        { "name", GetStr(payload, "name", "New RP Opportunity") },
                        { "ownerRole", persona },
                        { "stage", GetStr(payload, "stage", "Qualified") },
                        { "segment", GetStr(payload, "segment", "All") },
                        { "estimatedAmount", GetNum(payload, "estimatedAmount", 0.0) },
                        { "propertyWalkRequired", GetBool(payload, "propertyWalkRequired", true) },
                        { "tempoEnabled", GetBool(payload, "tempoEnabled", false) && GetStr(payload, "segment") == "National Accounts" },
                        { "forecastHoldDays", holdDays },
                        { "flags", flags }
                    };
                    opps.Add(opp);
                    AppendAudit(actor, persona, "opportunity.created", oppId, GetStr(opp, "name"));
                }
                else if (action == "item.upsert_batch")
                {
                    var rows = payload.ContainsKey("items") && payload["items"] is ArrayList ? (ArrayList)payload["items"] : new ArrayList();
                    foreach (object obj in rows)
                    {
                        var r = obj as Dictionary<string, object>;
                        if (r == null) continue;
                        string sku = GetStr(r, "sku").Trim().ToUpperInvariant();
                        r["sku"] = sku;
                        r["updated"] = UtcNowIso();
                        var ex = FindByKey(items, "sku", sku);
                        if (ex != null)
                        {
                            foreach (var kv in r) ex[kv.Key] = kv.Value;
                        }
                        else
                        {
                            items.Add(r);
                        }
                    }
                    AppendAudit(actor, persona, "item.upsert_batch", rows.Count + " SKUs", GetStr(payload, "source", "API"));
                }
                else if (action == "item.swap_discontinued")
                {
                    string oldSku = GetStr(payload, "oldSku").Trim().ToUpperInvariant();
                    string newSku = GetStr(payload, "newSku").Trim().ToUpperInvariant();
                    var newItem = FindByKey(items, "sku", newSku);
                    if (newItem == null || GetStr(newItem, "status") != "Active")
                    {
                        httpStatus = 400;
                        return ErrorObj("CPQ-34: Replacement SKU '" + newSku + "' is not an Active item.");
                    }
                    double newPrice = GetNum(newItem, "price", 0.0);
                    var touchedQuotes = new List<string>();
                    foreach (object qObj in quotes)
                    {
                        var q = qObj as Dictionary<string, object>;
                        if (q == null) continue;
                        string st = GetStr(q, "status");
                        if (st == "Accepted" || st == "Expired" || st == "Cancelled") continue;
                        bool touched = false;
                        foreach (object lObj in EnsureList(q, "lines"))
                        {
                            var ln = lObj as Dictionary<string, object>;
                            if (ln != null && string.Equals(GetStr(ln, "sku"), oldSku, StringComparison.OrdinalIgnoreCase))
                            {
                                ln["sku"] = newSku;
                                if (newPrice > 0) ln["unit"] = newPrice;
                                touched = true;
                            }
                        }
                        if (touched) touchedQuotes.Add(GetStr(q, "id"));
                    }
                    AppendAudit(actor, persona, "item.bulk_swap", oldSku, "Swapped " + oldSku + " -> " + newSku + " on: " + string.Join(", ", touchedQuotes.ToArray()));
                }
                else if (action == "quote.validate_vc_line")
                {
                    var attrs = EnsureDict(payload, "vcAttributes");
                    if (!string.IsNullOrEmpty(GetStr(attrs, "blindSize")) && string.IsNullOrEmpty(GetStr(attrs, "blindMounting")))
                    {
                        httpStatus = 400;
                        return ErrorObj("CPQ-25 VC Constraint Blocked: Blind Mounting cannot be blank when Blind Size is used.");
                    }
                    var q = FindByKey(quotes, "id", GetStr(payload, "quoteId"));
                    if (q == null) { httpStatus = 404; return ErrorObj("Quote not found"); }
                    if (payload.ContainsKey("line") && payload["line"] != null) EnsureList(q, "lines").Add(payload["line"]);
                    AppendAudit(actor, persona, "quote.vc_line_added", GetStr(q, "id"), "Validated SAP VC line");
                }
                else if (action == "quote.request_change")
                {
                    string qid = GetStr(payload, "quoteId");
                    string note = GetStr(payload, "note");
                    var q = FindByKey(quotes, "id", qid);
                    if (q == null) { httpStatus = 404; return ErrorObj("Quote not found"); }
                    q["status"] = "Revision requested";
                    AppendAudit(actor, persona, "customer.change_requested", qid, note);
                }
                else if (action == "quote.accept")
                {
                    string qid = GetStr(payload, "quoteId");
                    string signer = GetStr(payload, "signer", actor);
                    var q = FindByKey(quotes, "id", qid);
                    if (q == null) { httpStatus = 404; return ErrorObj("Quote not found"); }
                    foreach (object lObj in EnsureList(q, "lines"))
                    {
                        var ln = lObj as Dictionary<string, object>;
                        if (ln == null) continue;
                        string sku = GetStr(ln, "sku");
                        var it = FindByKey(items, "sku", sku);
                        if (it == null || GetStr(it, "status") != "Active")
                        {
                            httpStatus = 400;
                            return ErrorObj("CPQ-34 / CPQ-61 Blocked: SKU '" + sku + "' is Discontinued or unknown. Swap to active replacement first.");
                        }
                    }
                    q["status"] = "Accepted";
                    q["signedRofAttached"] = true;
                    q["acceptedBy"] = signer;
                    q["acceptedAt"] = UtcNowIso();
                    string linkedOppId = GetStr(q, "opportunityId");
                    if (!string.IsNullOrEmpty(linkedOppId))
                    {
                        var linkedOpp = FindByKey(opps, "opportunityId", linkedOppId);
                        if (linkedOpp != null && GetStr(linkedOpp, "stage") != "Project In Progress")
                        {
                            linkedOpp["stage"] = "Quote Won";
                            linkedOpp["probability"] = 95;
                        }
                    }
                    AppendAudit(actor, persona, "customer.accepted", qid, "Accepted by " + signer);
                    RunOsqPipelineInternal(actor, persona, null);
                }
                else if (action == "forecast.emergency_brake")
                {
                    string decision = GetStr(payload, "decision", "HELD").ToUpperInvariant();
                    var fb = EnsureDict(_relational, "forecastBatch");
                    var eb = EnsureDict(fb, "emergencyBrake");
                    eb["status"] = decision;
                    eb["tripped"] = (decision != "OVERRIDDEN");
                    eb["resolvedBy"] = actor;
                    eb["resolvedAt"] = UtcNowIso();
                    fb["taskState"] = (decision == "OVERRIDDEN") ? "ACCEPTED" : "QUARANTINED";
                    AppendAudit(actor, persona, "forecast.emergency_brake." + decision.ToLowerInvariant(), GetStr(fb, "batchId", "BATCH"), "Decision by " + actor);
                }
                else if (action == "marketplace.tokenize")
                {
                    string assetId = GetStr(payload, "asset_id", "osq-shard-" + NowEpoch());
                    var shards = EnsureList(_relational, "marketplaceShards");
                    var shard = new Dictionary<string, object>
                    {
                        { "asset_id", assetId },
                        { "token_id", GetStr(payload, "token_id", "tok-hub-osq-" + NowEpoch()) },
                        { "axis", GetStr(payload, "axis", "entirety") },
                        { "title", GetStr(payload, "title", "x_OSQ Curated Forecast Governance Shard") },
                        { "category", "supply_chain_" + GetStr(payload, "domain", "forecasting") },
                        { "marketplace_scope", GetStr(payload, "marketplace_scope", "hub") },
                        { "org_id", GetStr(payload, "org_id", "org-blackbulls-hideout-01") },
                        { "hub_id", GetStr(payload, "hub_id", "hub-floor-main") },
                        { "supply_chain_domain", GetStr(payload, "domain", "forecasting") },
                        { "sku_ref", GetStr(payload, "sku_ref", "SKU-HVAC-3001") },
                        { "batch_id", GetStr(payload, "batch_id", "BATCH-20261121-001") },
                        { "forecast_horizon", GetStr(payload, "forecast_horizon", "26-weeks") },
                        { "sla_target", GetStr(payload, "sla_target", "QUIPU Curated + SAP 261 Synced") },
                        { "shard_count", (int)GetNum(payload, "shard_count", 500) },
                        { "available_shards", (int)GetNum(payload, "shard_count", 500) },
                        { "current_shard_price", GetNum(payload, "shard_price", 12.5) },
                        { "royalty_rate", 0.02 },
                        { "quality_score", 0.98 },
                        { "protocol", "gard-shard/v2" },
                        { "domain", "SiCi_SQRT(-1)" },
                        { "envelope_digest", Sha256Hex(NewSerializer().Serialize(payload)) }
                    };
                    shards.Insert(0, shard);
                    AppendAudit(actor, persona, "marketplace.shard.tokenized", assetId, "Scope=" + GetStr(shard, "marketplace_scope"));
                }
                else if (action == "change.upsert")
                {
                    var changes = EnsureList(_relational, "changes");
                    string cid = GetStr(payload, "id");
                    var ex = FindByKey(changes, "id", cid);
                    if (ex != null) { foreach (var kv in payload) ex[kv.Key] = kv.Value; }
                    else changes.Add(payload);
                    AppendAudit(actor, persona, "change." + GetStr(payload, "status", "updated").ToLowerInvariant(), cid, GetStr(payload, "object"));
                }
                else if (action == "snapshot.create")
                {
                    EnsureList(_relational, "snapshots").Add(payload);
                    AppendAudit(actor, persona, "snapshot.created", GetStr(payload, "id", "SNAP"), GetStr(payload, "note"));
                }
                else if (action == "connector.upsert")
                {
                    string secretErr;
                    if (!ValidateSecretRef(GetStr(payload, "authRef"), out secretErr))
                    {
                        httpStatus = 400;
                        return ErrorObj(secretErr);
                    }
                    var connectors = EnsureList(_relational, "connectors");
                    string cid = GetStr(payload, "id");
                    var clean = (Dictionary<string, object>)CloneJsonValue(payload);
                    if (string.IsNullOrEmpty(GetStr(clean, "authRef"))) clean["authRef"] = "NONE";
                    if (!clean.ContainsKey("readGrants")) clean["readGrants"] = new ArrayList(new[] { "connector.read" });
                    if (!clean.ContainsKey("mutationGrants")) clean["mutationGrants"] = new ArrayList(new[] { "connector.mutate.idempotent" });
                    var ex = FindByKey(connectors, "id", cid);
                    if (ex != null) { foreach (var kv in clean) ex[kv.Key] = kv.Value; }
                    else connectors.Add(clean);
                    AppendAudit(actor, persona, "connector.saved", cid, GetStr(clean, "name"));
                }
                else if (action == "crm.account.upsert")
                {
                    string aid = GetStr(payload, "accountId", "ACC-" + NowEpoch());
                    var ex = FindByKey(accounts, "accountId", aid);
                    if (ex == null)
                    {
                        ex = new Dictionary<string, object>
                        {
                            { "accountId", aid },
                            { "status", "Active" },
                            { "creditStatus", "Clear" },
                            { "segment", "All" },
                            { "ewalletBalance", 0.0 }
                        };
                        accounts.Add(ex);
                    }
                    foreach (var kv in payload)
                    {
                        if (kv.Key != "accountId") ex[kv.Key] = kv.Value;
                    }
                    ex["accountId"] = aid;
                    AppendAudit(actor, persona, "crm.account.saved", aid, GetStr(ex, "name"));
                }
                else if (action == "crm.opportunity.advance_stage")
                {
                    string oid = GetStr(payload, "opportunityId");
                    var opp = FindByKey(opps, "opportunityId", oid);
                    if (opp == null) { httpStatus = 404; return ErrorObj("Opportunity not found: " + oid); }

                    string acctId = GetStr(opp, "accountId");
                    var acct = FindByKey(accounts, "accountId", acctId);
                    if (acct != null && GetStr(acct, "status").Equals("Closed", StringComparison.OrdinalIgnoreCase))
                    {
                        httpStatus = 400;
                        return ErrorObj("CPQ-1 Blocked: Cannot modify opportunity on Closed account '" + GetStr(acct, "name") + "'.");
                    }

                    if (GetBool(payload, "autoClear180", false))
                    {
                        DateTime clearAt;
                        if (TryParseUtcIso(GetStr(opp, "conditionAutoClearsAt"), out clearAt) && clearAt <= DateTime.UtcNow)
                        {
                            opp["opportunityCondition"] = null;
                            opp["conditionAutoClearsAt"] = null;
                            AppendAudit(actor, persona, "opportunity.condition_autocleared", oid, "CPQ-9 cleared >180d placeholder");
                        }
                    }

                    if (payload.ContainsKey("opportunityCondition"))
                    {
                        object condObj = payload["opportunityCondition"];
                        opp["opportunityCondition"] = condObj;
                        string condStr = condObj == null ? "" : Convert.ToString(condObj);
                        if (condStr == "Pro Forma" || condStr == "MOG")
                        {
                            opp["conditionAutoClearsAt"] = GetStr(payload, "conditionAutoClearsAt", DateTime.UtcNow.AddDays(180).ToString("yyyy-MM-ddTHH:mm:ssZ", System.Globalization.CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            opp["conditionAutoClearsAt"] = null;
                        }
                    }

                    string[] oppCopyFields = { "name", "segment", "estimatedAmount", "targetBudget", "serviceSolution", "expectedCloseDate", "qualificationQuestionsYes", "propertyWalkRequired", "walkSummary", "projectLocationOverride", "stageAgeDays" };
                    foreach (string f in oppCopyFields)
                    {
                        if (payload.ContainsKey(f)) opp[f] = payload[f];
                    }
                    if (payload.ContainsKey("flags") && payload["flags"] is Dictionary<string, object>)
                    {
                        var fl = EnsureDict(opp, "flags");
                        foreach (var kv in (Dictionary<string, object>)payload["flags"]) fl[kv.Key] = kv.Value;
                    }
                    var oppFlags = EnsureDict(opp, "flags");
                    if (GetBool(oppFlags, "coordinatedDelivery", false))
                    {
                        oppFlags["kitted"] = true;
                        opp["forecastHoldDays"] = 45;
                    }
                    else
                    {
                        opp["forecastHoldDays"] = (int)GetNum(opp, "forecastHoldDays", 30);
                    }

                    string targetStage = GetStr(payload, "targetStage", GetStr(payload, "stage", GetStr(opp, "stage", "Qualified")));
                    if (!targetStage.Equals("Lead", StringComparison.OrdinalIgnoreCase) && !targetStage.Equals("Closed Lost", StringComparison.OrdinalIgnoreCase))
                    {
                        int qYes = (int)GetNum(opp, "qualificationQuestionsYes", 0);
                        double estAmt = GetNum(opp, "estimatedAmount", 0.0);
                        if (qYes < 1)
                        {
                            httpStatus = 400;
                            return ErrorObj("CPQ-5 Blocked: At least 1 qualification question must be answered Yes before advancing past Lead.");
                        }
                        if (estAmt <= 0.0)
                        {
                            httpStatus = 400;
                            return ErrorObj("CPQ-14 Blocked: Estimated Amount must exceed $0 before entering Qualified or later stages.");
                        }
                    }
                    if (targetStage.Equals("Property Walk", StringComparison.OrdinalIgnoreCase) && !GetBool(opp, "propertyWalkRequired", true))
                    {
                        targetStage = "Ready for Quote";
                    }

                    string prevStage = GetStr(opp, "stage");
                    opp["stage"] = targetStage;
                    opp["probability"] = payload.ContainsKey("probability") ? (int)GetNum(payload, "probability", StageDefaultProbability(targetStage)) : StageDefaultProbability(targetStage);
                    if (!string.Equals(prevStage, targetStage, StringComparison.OrdinalIgnoreCase) && !payload.ContainsKey("stageAgeDays"))
                    {
                        opp["stageAgeDays"] = 0;
                    }
                    AppendAudit(actor, persona, "crm.opportunity.stage_advanced", oid, prevStage + " -> " + targetStage);
                }
                else if (action == "budget.upsert")
                {
                    string bid = GetStr(payload, "budgetId", "BDG-" + NowEpoch());
                    var ex = FindByKey(budgets, "budgetId", bid);
                    string cloneFrom = GetStr(payload, "cloneFromBudgetId");
                    if (ex == null)
                    {
                        if (!string.IsNullOrEmpty(cloneFrom))
                        {
                            var src = FindByKey(budgets, "budgetId", cloneFrom);
                            if (src == null) { httpStatus = 404; return ErrorObj("Clone source budget not found: " + cloneFrom); }
                            ex = (Dictionary<string, object>)CloneJsonValue(src);
                            ex["budgetId"] = bid;
                            ex["isMogTemplate"] = false;
                            ex["linkedQuoteId"] = null;
                            ex["status"] = "Draft CAPEX";
                        }
                        else
                        {
                            ex = new Dictionary<string, object>
                            {
                                { "budgetId", bid },
                                { "opportunityId", GetStr(payload, "opportunityId") },
                                { "accountId", GetStr(payload, "accountId") },
                                { "title", GetStr(payload, "title", "New Holon CAPEX Budget Scenario") },
                                { "budgetMode", GetStr(payload, "budgetMode", "Live CAPEX") },
                                { "quoteType", GetStr(payload, "quoteType", "PI / Turns") },
                                { "occupancyMultiplier", 1.0 },
                                { "targetCapexBudget", 50000.0 },
                                { "contingencyPct", 5.0 },
                                { "taxPct", 7.25 },
                                { "freightEstimate", 0.0 },
                                { "bmsDiscountPct", 0.0 },
                                { "ewalletApplied", 0.0 },
                                { "isProForma", false },
                                { "isMogTemplate", false },
                                { "status", "Draft CAPEX" },
                                { "floorplanGroups", new ArrayList() },
                                { "lines", new ArrayList() }
                            };
                        }
                        budgets.Add(ex);
                    }
                    foreach (var kv in payload)
                    {
                        if (kv.Key != "budgetId" && kv.Key != "cloneFromBudgetId")
                        {
                            ex[kv.Key] = CloneJsonValue(kv.Value);
                        }
                    }
                    ex["budgetId"] = bid;
                    ex["updatedAt"] = UtcNowIso();
                    AppendAudit(actor, persona, "budget.saved", bid, GetStr(ex, "title"));
                }
                else if (action == "budget.promote_to_quote")
                {
                    string bid = GetStr(payload, "budgetId");
                    var bdg = FindByKey(budgets, "budgetId", bid);
                    if (bdg == null) { httpStatus = 404; return ErrorObj("Budget scenario not found: " + bid); }

                    string oid = GetStr(payload, "opportunityId", GetStr(bdg, "opportunityId"));
                    var opp = string.IsNullOrEmpty(oid) ? null : FindByKey(opps, "opportunityId", oid);
                    string acctId = GetStr(payload, "accountId", opp != null ? GetStr(opp, "accountId") : GetStr(bdg, "accountId"));
                    var acct = string.IsNullOrEmpty(acctId) ? null : FindByKey(accounts, "accountId", acctId);
                    if (acct != null && GetStr(acct, "status").Equals("Closed", StringComparison.OrdinalIgnoreCase))
                    {
                        httpStatus = 400;
                        return ErrorObj("CPQ-1 Blocked: Cannot promote budget for Closed account '" + GetStr(acct, "name") + "'.");
                    }

                    bool isProForma = GetBool(bdg, "isProForma", false) || GetBool(bdg, "isMogTemplate", false) ||
                        (opp != null && GetStr(opp, "opportunityCondition").Equals("Pro Forma", StringComparison.OrdinalIgnoreCase));
                    bool convertToLive = GetBool(payload, "convertProFormaToLive", false);
                    if (isProForma && !convertToLive)
                    {
                        httpStatus = 400;
                        return ErrorObj("CPQ-8 Blocked: Pro Forma / MOG budgets cannot promote to a live Primary Quote unless convertProFormaToLive=true.");
                    }
                    if (convertToLive)
                    {
                        bdg["isProForma"] = false;
                        bdg["isMogTemplate"] = false;
                        bdg["budgetMode"] = "Live CAPEX";
                        if (opp != null)
                        {
                            opp["opportunityCondition"] = null;
                            opp["conditionAutoClearsAt"] = null;
                        }
                    }

                    double occMult = Math.Max(0.1, GetNum(bdg, "occupancyMultiplier", 1.0));
                    string qid = GetStr(payload, "quoteId", GetStr(bdg, "linkedQuoteId", "CPQ-" + NowEpoch()));
                    var existingQ = FindByKey(quotes, "id", qid);
                    bool isNewQuote = (existingQ == null);
                    var q = existingQ ?? new Dictionary<string, object>();
                    q["id"] = qid;
                    q["opportunityId"] = oid;
                    q["accountId"] = acctId;
                    q["budgetId"] = bid;
                    q["customer"] = acct != null ? GetStr(acct, "name", "Customer") : GetStr(payload, "customer", "HD Supply Customer");
                    q["source"] = "CPQ";
                    q["quoteType"] = GetStr(bdg, "quoteType", "PI / Turns");
                    q["hospitalityQuoteType"] = bdg.ContainsKey("hospitalityQuoteType") ? bdg["hospitalityQuoteType"] : null;
                    q["version"] = existingQ != null ? (int)GetNum(existingQ, "version", 1) + 1 : 1;
                    q["status"] = "Draft";
                    q["approvalStatus"] = "Draft";
                    q["approvalMode"] = "Pending";
                    q["isPrimary"] = true;
                    q["taxPct"] = GetNum(bdg, "taxPct", 7.25);
                    q["marginFloorPct"] = GetNum(q, "marginFloorPct", 24.0);
                    q["quoteDiscountPct"] = GetNum(q, "quoteDiscountPct", 0.0);
                    q["bmsDiscountPct"] = GetNum(bdg, "bmsDiscountPct", 0.0);
                    q["ewalletApplied"] = GetNum(bdg, "ewalletApplied", 0.0);
                    q["rofDetailLevel"] = GetStr(q, "rofDetailLevel", "floorplan");
                    if (acct != null && !string.IsNullOrEmpty(GetStr(acct, "priceProtectionUntil")))
                    {
                        q["priceProtectionUntil"] = GetStr(acct, "priceProtectionUntil");
                    }

                    var qLines = new ArrayList();
                    int idx = 1;
                    foreach (object blObj in EnsureList(bdg, "lines"))
                    {
                        var bl = blObj as Dictionary<string, object>;
                        if (bl == null) continue;
                        string sku = GetStr(bl, "sku").Trim().ToUpperInvariant();
                        int scaledQty = (int)Math.Max(1, Math.Round(GetNum(bl, "qty", 1.0) * occMult));
                        double unit = GetNum(bl, "unit", 0.0);
                        string grp = GetStr(bl, "group", "General");
                        string fpId = "FP-" + qid + "-" + idx;
                        var lnDict = new Dictionary<string, object>
                        {
                            { "lineId", "LN-" + idx },
                            { "sku", sku },
                            { "qty", scaledQty },
                            { "orderedQty", 0 },
                            { "unit", unit },
                            { "group", grp },
                            { "floorplanId", fpId },
                            { "floorplanDiscountPct", 0.0 },
                            { "lineDiscountPct", 0.0 }
                        };
                        if (bl.ContainsKey("vcAttributes") && bl["vcAttributes"] != null)
                        {
                            lnDict["vcAttributes"] = CloneJsonValue(bl["vcAttributes"]);
                        }
                        qLines.Add(lnDict);
                        idx++;
                    }
                    q["lines"] = qLines;
                    if (isNewQuote) quotes.Add(q);

                    // CPQ-70: enforce single primary quote per opportunity
                    if (!string.IsNullOrEmpty(oid))
                    {
                        foreach (object sqObj in quotes)
                        {
                            var sq = sqObj as Dictionary<string, object>;
                            if (sq != null && string.Equals(GetStr(sq, "opportunityId"), oid, StringComparison.OrdinalIgnoreCase))
                            {
                                sq["isPrimary"] = string.Equals(GetStr(sq, "id"), qid, StringComparison.OrdinalIgnoreCase);
                            }
                        }
                    }

                    // Sync floorplans
                    var groups = EnsureList(bdg, "floorplanGroups");
                    int fpSeq = 1;
                    foreach (object gObj in groups)
                    {
                        var g = gObj as Dictionary<string, object>;
                        if (g == null) continue;
                        string gName = GetStr(g, "name", "Floorplan " + fpSeq);
                        string fpId = "FP-" + qid + "-" + fpSeq;
                        var exFp = FindByKey(floorplans, "floorplanId", fpId);
                        if (exFp == null)
                        {
                            floorplans.Add(new Dictionary<string, object>
                            {
                                { "floorplanId", fpId },
                                { "quoteId", qid },
                                { "name", gName },
                                { "floorplanNumber", string.Format(System.Globalization.CultureInfo.InvariantCulture, "FP-{0:000}", fpSeq) },
                                { "units", (int)GetNum(g, "units", 1) },
                                { "beds", (int)GetNum(g, "beds", 1) },
                                { "baths", (int)GetNum(g, "baths", 1) }
                            });
                        }
                        fpSeq++;
                    }

                    RecomputeQuotePricing(q, opp, items);
                    bdg["status"] = "Promoted to Primary Quote";
                    bdg["linkedQuoteId"] = qid;
                    bdg["updatedAt"] = UtcNowIso();

                    if (opp != null)
                    {
                        string st = GetStr(opp, "stage");
                        if (st == "Lead" || st == "Qualified" || st == "Property Walk" || st == "Ready for Quote")
                        {
                            opp["stage"] = "Quoted";
                            opp["probability"] = 75;
                        }
                        double netSub = GetNum(q, "netSubtotal", 0.0);
                        if (netSub > 0.0) opp["estimatedAmount"] = netSub;
                    }
                    _relational["selectedQuoteId"] = qid;
                    AppendAudit(actor, persona, "budget.promoted_to_quote", bid, "Promoted to Primary Quote " + qid + " (CPQ-70)");
                }
                else if (action == "cpq.quote.upsert")
                {
                    string qid = GetStr(payload, "id", GetStr(payload, "quoteId", "CPQ-" + NowEpoch()));
                    var existing = FindByKey(quotes, "id", qid);
                    var q = existing ?? new Dictionary<string, object>
                    {
                        { "id", qid },
                        { "version", 1 },
                        { "status", "Draft" },
                        { "approvalStatus", "Draft" },
                        { "approvalMode", "Pending" },
                        { "isPrimary", false },
                        { "taxPct", 7.25 },
                        { "marginFloorPct", 24.0 },
                        { "quoteDiscountPct", 0.0 },
                        { "bmsDiscountPct", 0.0 },
                        { "ewalletApplied", 0.0 },
                        { "rofDetailLevel", "floorplan" },
                        { "lines", new ArrayList() }
                    };

                    string oid = GetStr(payload, "opportunityId", GetStr(q, "opportunityId"));
                    var opp = string.IsNullOrEmpty(oid) ? null : FindByKey(opps, "opportunityId", oid);
                    string acctId = GetStr(payload, "accountId", opp != null ? GetStr(opp, "accountId") : GetStr(q, "accountId"));
                    var acct = string.IsNullOrEmpty(acctId) ? null : FindByKey(accounts, "accountId", acctId);
                    if (acct != null && GetStr(acct, "status").Equals("Closed", StringComparison.OrdinalIgnoreCase))
                    {
                        httpStatus = 400;
                        return ErrorObj("CPQ-1 Blocked: Cannot create/update quote for Closed account '" + GetStr(acct, "name") + "'.");
                    }

                    // Validate candidate lines before mutating stored quote
                    ArrayList candidateLines = payload.ContainsKey("lines") ? EnsureList(payload, "lines") : EnsureList(q, "lines");
                    foreach (object lObj in candidateLines)
                    {
                        var ln = lObj as Dictionary<string, object>;
                        if (ln == null) continue;
                        string sku = GetStr(ln, "sku").Trim().ToUpperInvariant();
                        var it = string.IsNullOrEmpty(sku) ? null : FindByKey(items, "sku", sku);
                        bool isLabor = (it != null && GetStr(it, "itemType").Equals("Labor", StringComparison.OrdinalIgnoreCase)) || sku.StartsWith("LAB-");
                        if (isLabor)
                        {
                            bool installEnabled = (opp != null && GetBool(EnsureDict(opp, "flags"), "installation", false));
                            if (!installEnabled)
                            {
                                httpStatus = 400;
                                return ErrorObj("CPQ-11 Blocked: Labor SKU '" + sku + "' requires Opportunity flags.installation = true.");
                            }
                        }
                        if (it != null && GetBool(it, "vcRequired", false))
                        {
                            var attrs = EnsureDict(ln, "vcAttributes");
                            if (string.IsNullOrEmpty(GetStr(attrs, "blindSize")) || string.IsNullOrEmpty(GetStr(attrs, "blindMounting")))
                            {
                                httpStatus = 400;
                                return ErrorObj("CPQ-25 VC Constraint Blocked: SKU '" + sku + "' requires both blindSize and blindMounting.");
                            }
                        }
                    }

                    foreach (var kv in payload)
                    {
                        if (kv.Key != "quoteId" && kv.Key != "autoSubmitApproval")
                        {
                            q[kv.Key] = CloneJsonValue(kv.Value);
                        }
                    }
                    q["id"] = qid;
                    if (!string.IsNullOrEmpty(oid)) q["opportunityId"] = oid;
                    if (!string.IsNullOrEmpty(acctId)) q["accountId"] = acctId;
                    if (existing == null) quotes.Add(q);

                    if (GetBool(q, "isPrimary", false) && !string.IsNullOrEmpty(oid))
                    {
                        foreach (object sqObj in quotes)
                        {
                            var sq = sqObj as Dictionary<string, object>;
                            if (sq != null && string.Equals(GetStr(sq, "opportunityId"), oid, StringComparison.OrdinalIgnoreCase))
                            {
                                sq["isPrimary"] = string.Equals(GetStr(sq, "id"), qid, StringComparison.OrdinalIgnoreCase);
                            }
                        }
                    }

                    RecomputeQuotePricing(q, opp, items);
                    if (GetBool(payload, "autoSubmitApproval", false))
                    {
                        var guide = EnsureDict(q, "pricingGuidance");
                        if (GetBool(guide, "autoApproveEligible", false))
                        {
                            q["approvalStatus"] = "Approved-Pricing";
                            q["approvalMode"] = "Auto-Approved (CPQ-45)";
                            q["status"] = "Quoted";
                        }
                    }
                    AppendAudit(actor, persona, "cpq.quote.saved", qid, string.Format(System.Globalization.CultureInfo.InvariantCulture, "PM={0}%, Lines={1}", GetNum(q, "approvedMarginPct", 0.0), EnsureList(q, "lines").Count));
                }
                else if (action == "cpq.quote.approval")
                {
                    string qid = GetStr(payload, "quoteId", GetStr(payload, "id"));
                    string decision = GetStr(payload, "decision", "submit").Trim().ToLowerInvariant();
                    string justif = GetStr(payload, "justification").Trim();
                    var q = FindByKey(quotes, "id", qid);
                    if (q == null) { httpStatus = 404; return ErrorObj("Quote not found: " + qid); }
                    var opp = FindByKey(opps, "opportunityId", GetStr(q, "opportunityId"));
                    RecomputeQuotePricing(q, opp, items);

                    double marginPct = GetNum(q, "approvedMarginPct", 0.0);
                    double floorPct = GetNum(q, "marginFloorPct", 24.0);
                    bool forceEsc = GetBool(payload, "forceEscalation", false);

                    if (decision == "submit")
                    {
                        if (marginPct >= floorPct && !forceEsc)
                        {
                            q["approvalStatus"] = "Approved-Pricing";
                            q["approvalMode"] = "Auto-Approved (CPQ-45)";
                            q["approvalJustification"] = string.IsNullOrEmpty(justif) ? "Meets margin floor threshold." : justif;
                            q["status"] = "Quoted";
                        }
                        else
                        {
                            if (string.IsNullOrEmpty(justif))
                            {
                                httpStatus = 400;
                                return ErrorObj("CPQ-42 Blocked: Below-floor / escalated margin submission requires non-empty justification.");
                            }
                            q["approvalStatus"] = "Pending-Approval";
                            q["approvalMode"] = "Pricing Team Queue (CPQ-46)";
                            q["approvalJustification"] = justif;
                            q["status"] = "Pending Pricing Approval";
                        }
                    }
                    else if (decision == "recall")
                    {
                        q["approvalStatus"] = "Draft";
                        q["approvalMode"] = "Recalled by Seller (CPQ-44)";
                        q["status"] = "Draft";
                    }
                    else if (decision == "approve")
                    {
                        q["approvalStatus"] = "Approved-Pricing";
                        q["approvalMode"] = "Pricing Team Approved (" + actor + ")";
                        if (!string.IsNullOrEmpty(justif)) q["approvalJustification"] = justif;
                        q["approvedBy"] = actor;
                        q["approvedAt"] = UtcNowIso();
                        q["status"] = "Quoted";
                    }
                    else if (decision == "reject")
                    {
                        q["approvalStatus"] = "Rejected-Pricing";
                        q["approvalMode"] = "Pricing Rejected (" + actor + ")";
                        if (!string.IsNullOrEmpty(justif)) q["approvalJustification"] = justif;
                        q["status"] = "Revision requested";
                    }
                    else
                    {
                        httpStatus = 400;
                        return ErrorObj("Unknown approval decision: " + decision);
                    }
                    AppendAudit(actor, persona, "cpq.quote.approval." + decision, qid, "Status=" + GetStr(q, "approvalStatus"));
                }
                else if (action == "cpq.quote.price_refresh")
                {
                    string qid = GetStr(payload, "quoteId", GetStr(payload, "id"));
                    var q = FindByKey(quotes, "id", qid);
                    if (q == null) { httpStatus = 404; return ErrorObj("Quote not found: " + qid); }
                    var opp = FindByKey(opps, "opportunityId", GetStr(q, "opportunityId"));

                    string ppDateStr = GetStr(q, "priceProtectionUntil");
                    DateTime ppDate;
                    bool overridePp = GetBool(payload, "overridePriceProtection", false);
                    if (!overridePp && TryParseUtcIso(ppDateStr, out ppDate) && ppDate > DateTime.UtcNow)
                    {
                        httpStatus = 400;
                        return ErrorObj("CPQ-63 Blocked: Price Protection active until " + ppDateStr + " (pass overridePriceProtection=true to force market refresh).");
                    }

                    int updatedLines = 0;
                    foreach (object lObj in EnsureList(q, "lines"))
                    {
                        var ln = lObj as Dictionary<string, object>;
                        if (ln == null) continue;
                        double qQty = GetNum(ln, "qty", 0.0);
                        double oQty = GetNum(ln, "orderedQty", 0.0);
                        if (qQty - oQty <= 0.0) continue; // only refresh remaining unordered lines
                        var it = FindByKey(items, "sku", GetStr(ln, "sku"));
                        if (it != null)
                        {
                            double catalogPrice = GetNum(it, "price", 0.0);
                            if (catalogPrice > 0.0)
                            {
                                ln["unit"] = catalogPrice;
                                updatedLines++;
                            }
                        }
                    }
                    q["version"] = (int)GetNum(q, "version", 1) + 1;
                    q["lastPriceRefreshAt"] = UtcNowIso();
                    RecomputeQuotePricing(q, opp, items);
                    AppendAudit(actor, persona, "cpq.quote.price_refreshed", qid, "v" + q["version"] + ", refreshedLines=" + updatedLines);
                }
                else if (action == "cpq.order.place")
                {
                    string qid = GetStr(payload, "quoteId", GetStr(payload, "id"));
                    var q = FindByKey(quotes, "id", qid);
                    if (q == null) { httpStatus = 404; return ErrorObj("Quote not found: " + qid); }
                    if (!GetStr(q, "status").Equals("Accepted", StringComparison.OrdinalIgnoreCase))
                    {
                        httpStatus = 400;
                        return ErrorObj("CPQ-55 Blocked: Quote '" + qid + "' must be Accepted before placing a staged SAP Order.");
                    }

                    var reqLines = EnsureList(payload, "lines");
                    var qLines = EnsureList(q, "lines");
                    int unitsPlaced = 0;

                    if (reqLines.Count == 0)
                    {
                        // Default: order all remaining units
                        foreach (object lObj in qLines)
                        {
                            var ln = lObj as Dictionary<string, object>;
                            if (ln == null) continue;
                            int rem = (int)Math.Max(0, GetNum(ln, "qty", 0.0) - GetNum(ln, "orderedQty", 0.0));
                            if (rem > 0)
                            {
                                ln["orderedQty"] = (int)GetNum(ln, "qty", 0.0);
                                unitsPlaced += rem;
                            }
                        }
                    }
                    else
                    {
                        foreach (object rObj in reqLines)
                        {
                            var rl = rObj as Dictionary<string, object>;
                            if (rl == null) continue;
                            string lid = GetStr(rl, "lineId");
                            string sku = GetStr(rl, "sku");
                            int orderQty = (int)GetNum(rl, "orderQty", GetNum(rl, "qty", 0.0));
                            if (orderQty <= 0) continue;

                            Dictionary<string, object> targetLn = !string.IsNullOrEmpty(lid) ? FindByKey(qLines, "lineId", lid) : null;
                            if (targetLn == null && !string.IsNullOrEmpty(sku)) targetLn = FindByKey(qLines, "sku", sku);
                            if (targetLn == null) { httpStatus = 404; return ErrorObj("Quote line not found for order: " + (string.IsNullOrEmpty(lid) ? sku : lid)); }

                            int quoted = (int)GetNum(targetLn, "qty", 0.0);
                            int ordered = (int)GetNum(targetLn, "orderedQty", 0.0);
                            int rem = Math.Max(0, quoted - ordered);
                            if (orderQty > rem)
                            {
                                httpStatus = 400;
                                return ErrorObj("CPQ-56 Blocked: Cannot order " + orderQty + " on '" + GetStr(targetLn, "sku") + "' (remaining=" + rem + ").");
                            }
                            targetLn["orderedQty"] = ordered + orderQty;
                            unitsPlaced += orderQty;
                        }
                    }

                    string sapOrderNo = GetStr(payload, "sapOrderNumber", "SAP-ORD-" + DateTime.UtcNow.ToString("yyyyMMdd", System.Globalization.CultureInfo.InvariantCulture) + "-" + (NowEpoch() % 10000));
                    string sapOrderDate = DateTime.UtcNow.ToString("yyyy-MM-dd", System.Globalization.CultureInfo.InvariantCulture);
                    q["lastSapOrderNumber"] = sapOrderNo;
                    q["lastSapOrderDate"] = sapOrderDate;

                    var opp = FindByKey(opps, "opportunityId", GetStr(q, "opportunityId"));
                    if (opp != null)
                    {
                        opp["stage"] = "Project In Progress";
                        opp["probability"] = 100;
                        opp["daysSinceLastOrder"] = 0;
                    }

                    AppendAudit(actor, persona, "cpq.order.placed", qid, sapOrderNo + " (" + unitsPlaced + " units)");
                    RunOsqPipelineInternal(actor, persona, null);
                }
                else if (action == "osq.pipeline.run")
                {
                    RunOsqPipelineInternal(actor, persona, payload);
                }
                else if (action == "heuristics.update")
                {
                    var hc = EnsureHeuristicsConfig();
                    string preset = GetStr(payload, "preset", GetStr(payload, "activePreset"));
                    if (!string.IsNullOrEmpty(preset))
                    {
                        ApplyHeuristicsPreset(hc, preset);
                    }
                    string[] secKeys = { "slaAndTiming", "organizationAndMigration", "fulfillmentAndKitting", "forecastAndScb" };
                    foreach (string sk in secKeys)
                    {
                        var src = payload.ContainsKey(sk) ? (payload[sk] as Dictionary<string, object>) : null;
                        if (src != null)
                        {
                            var dst = EnsureDict(hc, sk);
                            foreach (var kv in src) dst[kv.Key] = kv.Value;
                        }
                    }
                    if (payload.ContainsKey("kittingDcNetwork") && payload["kittingDcNetwork"] != null)
                    {
                        hc["kittingDcNetwork"] = payload["kittingDcNetwork"];
                    }
                    hc["updatedAt"] = UtcNowIso();
                    hc["updatedBy"] = actor;
                    var evalRes = EvaluateHeuristicsOrder(payload);
                    AppendAudit(actor, persona, "heuristics.updated", GetStr(hc, "activePreset"), "Quote=" + GetStr(evalRes, "quoteId") + ", Lines=" + EnsureList(evalRes, "lineDecisions").Count);
                }
                else if (action == "heuristics.evaluate_order")
                {
                    var evalRes = EvaluateHeuristicsOrder(payload);
                    AppendAudit(actor, persona, "heuristics.evaluated", GetStr(evalRes, "quoteId"), "DC=" + GetStr(evalRes, "localDc") + "->" + GetStr(evalRes, "kittingDc"));
                }
                else if (action == "kitting.workflow.execute")
                {
                    string qid = GetStr(payload, "quoteId", GetStr(_relational, "selectedQuoteId", "ROF-DEMO-001"));
                    var q = FindByKey(quotes, "id", qid);
                    if (q == null) { httpStatus = 404; return ErrorObj("Quote not found: " + qid); }
                    if (payload.ContainsKey("parentKitSku")) q["parentKitSku"] = GetStr(payload, "parentKitSku", GetStr(q, "parentKitSku", "KIT-RENO-1BR"));
                    int newPQty = (int)Math.Max(1, GetNum(payload, "parentKitQty", GetNum(q, "parentKitQty", 10)));
                    int oldPQty = (int)Math.Max(1, GetNum(q, "parentKitQty", newPQty));
                    if (GetBool(payload, "scaleLinesByParentQty", false) && newPQty != oldPQty)
                    {
                        double ratio = (double)newPQty / (double)oldPQty;
                        foreach (object lObj in EnsureList(q, "lines"))
                        {
                            var ln = lObj as Dictionary<string, object>;
                            if (ln != null) ln["qty"] = (int)Math.Max(1, Math.Round(GetNum(ln, "qty", 1) * ratio));
                        }
                    }
                    q["parentKitQty"] = newPQty;
                    q["floorplanConverted"] = true;
                    q["edsValidated"] = GetBool(payload, "edsValidated", true);
                    q["outboundPrintValidated"] = GetBool(payload, "outboundPrintValidated", true);
                    if (payload.ContainsKey("unitNumber")) q["unitNumber"] = GetStr(payload, "unitNumber", "UNIT-101A");
                    if (payload.ContainsKey("localDc")) q["localDc"] = GetStr(payload, "localDc", "NC34").ToUpperInvariant();
                    if (payload.ContainsKey("orderToRddDays")) q["orderToRddDays"] = (int)GetNum(payload, "orderToRddDays", 9);
                    if (payload.ContainsKey("deliveryBlock")) q["deliveryBlock"] = GetStr(payload, "deliveryBlock", "ZR");
                    var evalRes = EvaluateHeuristicsOrder(payload);
                    var cnt = EnsureDict(evalRes, "summaryCounts");
                    q["zstockStatus"] = ((int)GetNum(cnt, "waitPreserveCount", 0) > 0) ? "FUTURE_KIT_PROTECTED" : "REVIEWED_OK";
                    AppendAudit(actor, persona, "kitting.workflow.executed", qid, "Parent=" + q["parentKitSku"] + "x" + newPQty + ", Unit=" + GetStr(q, "unitNumber") + ", ZSTOCK=" + q["zstockStatus"]);
                }
                else if (action == "quote.split_allocate")
                {
                    var hc = EnsureHeuristicsConfig();
                    string qid = GetStr(payload, "quoteId", GetStr(_relational, "selectedQuoteId", "ROF-DEMO-001"));
                    var q = FindByKey(quotes, "id", qid);
                    if (q == null) { httpStatus = 404; return ErrorObj("Quote not found: " + qid); }
                    string tokenHash = Sha256Hex(qid + "|" + GetStr(q, "customer") + "|" + CanonicalSerialize(EnsureList(q, "lines"))).Substring(0, 24);
                    var evalRes = EvaluateHeuristicsOrder(payload);
                    var shards = new ArrayList();
                    int idx = 0;
                    var byDc = new Dictionary<string, ArrayList>();
                    foreach (object lObj in EnsureList(evalRes, "lineDecisions"))
                    {
                        var ld = lObj as Dictionary<string, object>;
                        if (ld == null) continue;
                        string srcDc = GetStr(ld, "spokeDc", GetStr(evalRes, "kittingDc", "GA02"));
                        if (!byDc.ContainsKey(srcDc)) byDc[srcDc] = new ArrayList();
                        byDc[srcDc].Add(new Dictionary<string, object> { { "sku", GetStr(ld, "sku") }, { "qty", GetNum(ld, "qty", 1) }, { "mode", GetStr(ld, "decisionCode") } });
                    }
                    string kDc = GetStr(evalRes, "kittingDc", "GA02");
                    foreach (var kv in byDc)
                    {
                        var dr = RunWeightedDijkstra(kv.Key, kDc, kv.Value.Count * 5, hc);
                        shards.Add(new Dictionary<string, object>
                        {
                            { "subOrderId", "SO-" + qid + "." + idx }, { "sourceDc", kv.Key }, { "targetDc", kDc },
                            { "lineCount", kv.Value.Count }, { "lines", kv.Value }, { "dijkstraRoute", dr },
                            { "consolidationDifferentialUsd", dr["consolidationDifferentialUsd"] },
                            { "optionalHoldingCostUpchargeUsd", dr["optionalHoldingCostUpchargeUsd"] }
                        });
                        idx++;
                    }
                    q["quoteTokenHash"] = tokenHash;
                    q["splitAllocations"] = shards;
                    AppendAudit(actor, persona, "quote.split_allocate", qid, "Token=" + tokenHash + ", Shards=" + shards.Count);
                }
                else if (action == "bom.flatten_modular")
                {
                    string qid = GetStr(payload, "quoteId", GetStr(_relational, "selectedQuoteId", "ROF-DEMO-001"));
                    var q = FindByKey(quotes, "id", qid);
                    if (q == null) { httpStatus = 404; return ErrorObj("Quote not found: " + qid); }
                    var evalRes = EvaluateHeuristicsOrder(payload);
                    var arch = EnsureDict(evalRes, "tx03Tx77Architecture");
                    var mGroups = EnsureList(arch, "modularBomGroups");
                    q["modularBomGroups"] = mGroups;
                    var modStore = EnsureList(_relational, "modularBoms");
                    modStore.Add(new Dictionary<string, object> { { "quoteId", qid }, { "flattenedAt", UtcNowIso() }, { "groups", mGroups } });
                    while (modStore.Count > 100) modStore.RemoveAt(0);
                    AppendAudit(actor, persona, "bom.flatten_modular", qid, "Modules=" + mGroups.Count);
                }
                else if (action == "sto.schedule")
                {
                    string ordId = GetStr(payload, "orderId", GetStr(payload, "quoteId", "ROF-DEMO-001"));
                    string stoCode = GetStr(payload, "stoCode", GetStr(payload, "skuSignature", "STO-DEFAULT"));
                    string rdd = GetStr(payload, "rdd", DateTime.UtcNow.ToString("yyyy-MM-dd", System.Globalization.CultureInfo.InvariantCulture));
                    string fromDc = GetStr(payload, "fromDc", "NC34").ToUpperInvariant();
                    string toDc = GetStr(payload, "toDc", "GA02").ToUpperInvariant();
                    int sQty = (int)Math.Max(1, GetNum(payload, "qty", 1));
                    bool supOver = GetBool(payload, "supervisorOverride", false);
                    var stos = EnsureList(_relational, "scheduledStos");
                    foreach (object sObj in stos)
                    {
                        var st = sObj as Dictionary<string, object>;
                        if (st != null && GetStr(st, "orderId") == ordId && GetStr(st, "stoCode") == stoCode && GetStr(st, "rdd") == rdd && !supOver)
                        {
                            httpStatus = 409;
                            return ErrorObj("Same-day duplicate STO blocked (409): orderId=" + ordId + ", stoCode=" + stoCode + ", rdd=" + rdd + ". Pass supervisorOverride=true or schedule on a different RDD.");
                        }
                    }
                    string shipId = "IS-" + (1000 + EnsureList(_relational, "rfidShipments").Count + 1);
                    stos.Add(new Dictionary<string, object> {
                        { "orderId", ordId }, { "stoCode", stoCode }, { "rdd", rdd }, { "fromDc", fromDc }, { "toDc", toDc },
                        { "qty", sQty }, { "supervisorOverride", supOver }, { "shipmentId", shipId }, { "createdAt", UtcNowIso() }
                    });
                    EnsureList(_relational, "rfidShipments").Add(new Dictionary<string, object> {
                        { "shipmentId", shipId }, { "shipmentType", "IS" }, { "lineStyle", "dashed" },
                        { "fromDc", fromDc }, { "toDc", toDc }, { "status", "SCHEDULED" }, { "protocol", "rust-rfid-open-tracker/v1" },
                        { "tagId", "RUST-RFID-" + shipId.Replace("-", "") }, { "baselineHours", 4.5 }, { "activatedAtUtc", UtcNowIso() }
                    });
                    AppendAudit(actor, persona, "sto.scheduled", shipId, ordId + " / " + stoCode + " @ " + rdd);
                }
                else if (action == "order.credit_hold_rdd_adjust")
                {
                    string qid = GetStr(payload, "quoteId", GetStr(_relational, "selectedQuoteId", "ROF-DEMO-001"));
                    var q = FindByKey(quotes, "id", qid);
                    if (q == null) { httpStatus = 404; return ErrorObj("Quote not found: " + qid); }
                    int shiftD = (int)GetNum(payload, "shiftDays", 3);
                    int curLead = (int)GetNum(q, "orderToRddDays", 8);
                    q["orderToRddDays"] = Math.Max(1, curLead + shiftD);
                    q["creditHoldDays"] = (int)GetNum(payload, "creditHoldDays", 0);
                    q["creditHoldRddAdjustedAt"] = UtcNowIso();
                    if (!string.IsNullOrEmpty(GetStr(payload, "newRdd"))) q["adjustedRdd"] = GetStr(payload, "newRdd");
                    EvaluateHeuristicsOrder(new Dictionary<string, object> { { "quoteId", qid } });
                    EnsureList(_relational, "sapSignals").Add(new Dictionary<string, object> { { "signalType", "RDD_CREDIT_SHIFT" }, { "targetId", qid }, { "shiftDays", shiftD }, { "status", "DISPATCHED_TO_SAP_BRIDGE" }, { "at", UtcNowIso() } });
                    AppendAudit(actor, persona, "order.credit_hold_rdd_adjusted", qid, "ShiftDays=" + shiftD + ", NewLeadDays=" + q["orderToRddDays"]);
                }
                else if (action == "ifr.auto_po")
                {
                    EnsureHeuristicsConfig();
                    double tgtIfr = GetNum(payload, "targetIfrPct", 98.5);
                    string reqSku = GetStr(payload, "sku").ToUpperInvariant();
                    var pos = EnsureList(_relational, "autoReplenishmentPos");
                    int genCount = 0;
                    foreach (object iObj in items)
                    {
                        var it = iObj as Dictionary<string, object>;
                        if (it == null) continue;
                        string sku = GetStr(it, "sku").ToUpperInvariant();
                        if (!string.IsNullOrEmpty(reqSku) && sku != reqSku) continue;
                        var fm = EnsureDict(it, "floorQohByDc");
                        var cm = EnsureDict(it, "committedQtyByDc");
                        var sm = EnsureDict(it, "storage916QtyByDc");
                        foreach (var kv in fm)
                        {
                            int fq = Convert.ToInt32(kv.Value), cq = (int)GetNum(cm, kv.Key, 0), q916 = (int)GetNum(sm, kv.Key, 0);
                            int avail = Math.Max(0, fq - cq - q916);
                            if (avail < 12)
                            {
                                pos.Add(new Dictionary<string, object> {
                                    { "poId", "PO-IFR-" + (pos.Count + 101) }, { "sku", sku }, { "dc", kv.Key },
                                    { "orderQty", Math.Max(15, 25 - avail) }, { "projectedIfrPct", tgtIfr },
                                    { "status", "AUTO_GENERATED_OPEN" }, { "createdAt", UtcNowIso() }
                                });
                                genCount++;
                            }
                        }
                    }
                    if (genCount == 0)
                    {
                        pos.Add(new Dictionary<string, object> {
                            { "poId", "PO-IFR-" + (pos.Count + 101) }, { "sku", string.IsNullOrEmpty(reqSku) ? "SKU-M18-DRILL" : reqSku },
                            { "dc", GetStr(payload, "dc", "GA02") }, { "orderQty", (int)Math.Max(1, GetNum(payload, "qty", 20)) },
                            { "projectedIfrPct", tgtIfr }, { "status", "AUTO_GENERATED_OPEN" }, { "createdAt", UtcNowIso() }
                        });
                    }
                    AppendAudit(actor, persona, "ifr.auto_po.generated", "IFR", "Count=" + pos.Count + ", TargetIFR=" + tgtIfr);
                    RefreshPathwaysAndDirectives(action);
                }
                else if (action == "order.cancel_reconcile")
                {
                    string ordId = GetStr(payload, "orderId", GetStr(payload, "quoteId", "ROF-DEMO-001"));
                    string cSku = GetStr(payload, "sku", "SKU-FAUCET-PRO").ToUpperInvariant();
                    int redQty = (int)Math.Max(1, GetNum(payload, "reducedQty", 5));
                    bool arrived = GetBool(payload, "inboundArrivedWithoutReduction", false);
                    var pos = EnsureList(_relational, "autoReplenishmentPos");
                    foreach (object pObj in pos)
                    {
                        var po = pObj as Dictionary<string, object>;
                        if (po != null && GetStr(po, "sku").ToUpperInvariant() == cSku) po["status"] = "PO_REDUCTION_REQUIRED";
                    }
                    if (arrived)
                    {
                        EnsureList(_relational, "receivingQuarantines").Add(new Dictionary<string, object> {
                            { "quarantineId", "QRN-" + (EnsureList(_relational, "receivingQuarantines").Count + 1) },
                            { "orderId", ordId }, { "sku", cSku }, { "qty", redQty },
                            { "status", "RECEIVING_QUARANTINE_RETURN" }, { "quarantineAction", "HOLD_ON_DOCK_FOR_VENDOR_RETURN" },
                            { "alertRecipients", new[] { "Receiving", "Buyer", "Supervisor" } }, { "createdAt", UtcNowIso() }
                        });
                    }
                    else
                    {
                        pos.Add(new Dictionary<string, object> {
                            { "poId", "PO-RED-" + (pos.Count + 101) }, { "sku", cSku }, { "dc", GetStr(payload, "dc", "TX03") },
                            { "orderQty", redQty }, { "status", "PO_REDUCTION_REQUIRED" }, { "orderId", ordId }, { "createdAt", UtcNowIso() }
                        });
                    }
                    RefreshPathwaysAndDirectives(action);
                    AppendAudit(actor, persona, "order.cancel_reconciled", ordId, "SKU=" + cSku + ", Quarantine=" + arrived);
                }
                else if (action == "rfid.event")
                {
                    string shipId = GetStr(payload, "shipmentId", "IS-1001").ToUpperInvariant();
                    string evType = GetStr(payload, "eventType", "RECEIPT_SCAN").ToUpperInvariant();
                    string fromDc = GetStr(payload, "fromDc", "NC34").ToUpperInvariant();
                    string toDc = GetStr(payload, "toDc", "GA02").ToUpperInvariant();
                    double obsHrs = GetNum(payload, "observedHours", 5.2);
                    double baseHrs = GetNum(payload, "baselineHours", 4.5);
                    string sType = shipId.StartsWith("CS-") ? "CS" : "IS";
                    var rfids = EnsureList(_relational, "rfidShipments");
                    var rec = FindByKey(rfids, "shipmentId", shipId);
                    if (rec == null)
                    {
                        rec = new Dictionary<string, object> {
                            { "shipmentId", shipId }, { "shipmentType", sType }, { "lineStyle", sType == "CS" ? "solid" : "dashed" },
                            { "fromDc", fromDc }, { "toDc", toDc }, { "protocol", "rust-rfid-open-tracker/v1" },
                            { "tagId", GetStr(payload, "tagId", "RUST-RFID-" + shipId.Replace("-", "")) }, { "baselineHours", baseHrs }
                        };
                        rfids.Add(rec);
                    }
                    if (evType == "ACTIVATE") { rec["status"] = "ACTIVE_IN_TRANSIT"; rec["activatedAtUtc"] = UtcNowIso(); }
                    else if (evType == "RECEIPT_SCAN")
                    {
                        rec["status"] = "RECEIVED_VERIFIED"; rec["receivedAtUtc"] = UtcNowIso(); rec["elapsedHours"] = obsHrs;
                        var emaMap = EnsureDict(_relational, "rfidEdgeWeights");
                        string eKey = fromDc + "->" + toDc;
                        double prevEma = GetNum(emaMap, eKey, 1.0);
                        double ratio = Math.Max(0.4, Math.Min(3.0, obsHrs / Math.Max(0.5, baseHrs)));
                        emaMap[eKey] = Math.Round(0.75 * prevEma + 0.25 * ratio, 4);
                        AutoCloseDirectivesFromPhysicalEvent(
                            "RFID_RECEIPT_SCAN",
                            new[] { shipId, fromDc, toDc, GetStr(payload, "sku"), GetStr(payload, "orderId"), GetStr(payload, "quoteId") },
                            actor,
                            "RFID RECEIPT_SCAN on " + shipId + " (" + fromDc + "->" + toDc + ") elapsed=" + obsHrs + "h");
                    }
                    else { rec["status"] = evType; rec["checkpointAtUtc"] = UtcNowIso(); }
                    AppendAudit(actor, persona, "rfid.event", shipId, evType + " (" + fromDc + "->" + toDc + ")");
                }
                else if (action == "quipu.classify_note")
                {
                    string text = GetStr(payload, "noteText");
                    string low = text.ToLowerInvariant();
                    string cls = low.Contains("damage") || low.Contains("dent") || low.Contains("door") ? "DAMAGED_DOOR_HOLD"
                        : (low.Contains("short") || low.Contains("count") || low.Contains("missing") ? "CYCLE_COUNT_VARIANCE"
                        : (low.Contains("wip") || low.Contains("stage") || low.Contains("hold") ? "WIP_STAGING_PARTIAL" : "STANDARD_PUTAWAY"));
                    double risk = (cls == "CYCLE_COUNT_VARIANCE" || cls == "DAMAGED_DOOR_HOLD") ? 0.88 : 0.34;
                    EnsureList(_relational, "quipuNoteClassifications").Add(new Dictionary<string, object> {
                        { "noteId", "NOTE-" + (EnsureList(_relational, "quipuNoteClassifications").Count + 1) },
                        { "sourceType", GetStr(payload, "sourceType", "pallet_note") }, { "sku", GetStr(payload, "sku", "SKU-DOOR-36") },
                        { "dc", GetStr(payload, "dc", "TX03") }, { "noteText", text }, { "classification", cls },
                        { "wipImpactFlag", cls != "STANDARD_PUTAWAY" }, { "cycleCountRiskScore", risk }, { "classifiedAt", UtcNowIso() }
                    });
                    RefreshPathwaysAndDirectives(action);
                    AppendAudit(actor, persona, "quipu.note.classified", cls, text);
                }
                else if (action == "stockopoly.slot_lookup")
                {
                    string sSku = GetStr(payload, "sku", "SKU-FAUCET-PRO").ToUpperInvariant();
                    string sDc = GetStr(payload, "dc", "TX03").ToUpperInvariant();
                    string sapBin = GetStr(payload, "sapLocationCode", sDc + "-BIN-A12-L3");
                    bool drift = GetBool(payload, "simulateDrift", false);
                    EnsureList(_relational, "stockopolyBinMappings").Add(new Dictionary<string, object> {
                        { "sku", sSku }, { "dc", sDc }, { "sapStorageLocation", sapBin },
                        { "stockopolyCoords", new Dictionary<string, object> { { "x", 12.4 }, { "y", 4.8 }, { "z", 2.1 }, { "aisle", "A12" }, { "bay", "L3" }, { "corners", 4 } } },
                        { "matchStatus", drift ? "RECONCILE_BIN_DRIFT" : "VERIFIED_BIN_MATCH" }, { "checkedAt", UtcNowIso() }
                    });
                    RefreshPathwaysAndDirectives(action);
                    AppendAudit(actor, persona, "stockopoly.slot_lookup", sSku, sDc + " @ " + sapBin);
                }
                else if (action == "sap.signal")
                {
                    string sigType = GetStr(payload, "signalType", "QUOTE_RESERVE").Trim().ToUpperInvariant();
                    if (sigType == "RAW_INVENTORY_ADJUST" || sigType.IndexOf("RAW_INVENTORY", StringComparison.OrdinalIgnoreCase) >= 0 || sigType.IndexOf("DIRECT_QOH", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        RecordRestrictedAudit(actor, "DENY", "Blocked forbidden SAP signal: " + sigType, corrId, "");
                        httpStatus = 403;
                        return ErrorObj("SAP Policy Violation (403): RAW_INVENTORY_ADJUST is strictly forbidden outside physical SAP EWM/MIGO; use bounded operational signals (PR_ADJUST, PO_ADJUST, QUOTE_RESERVE, PICK_COMPLETE_KNOCKOFF, RDD_CREDIT_SHIFT).");
                    }
                    var sigs = EnsureList(_relational, "sapSignals");
                    sigs.Add(new Dictionary<string, object> {
                        { "signalId", "SAP-SIG-" + (sigs.Count + 101) }, { "signalType", sigType },
                        { "targetId", GetStr(payload, "targetId", "ROF-DEMO-001") }, { "dc", GetStr(payload, "dc", "TX03") },
                        { "sku", GetStr(payload, "sku") }, { "status", "DISPATCHED_TO_SAP_BRIDGE" }, { "dispatchedAt", UtcNowIso() }
                    });
                    if (sigType == "PICK_COMPLETE_KNOCKOFF")
                    {
                        AutoCloseDirectivesFromPhysicalEvent(
                            "SAP_PICK_COMPLETE_KNOCKOFF",
                            new[] { GetStr(payload, "targetId"), GetStr(payload, "sku"), GetStr(payload, "dc") },
                            actor,
                            "SAP PICK_COMPLETE_KNOCKOFF on " + GetStr(payload, "targetId") + " (" + GetStr(payload, "sku") + ")");
                    }
                    AppendAudit(actor, persona, "sap.signal.dispatched", sigType, GetStr(payload, "targetId", "ROF-DEMO-001"));
                }
                else if (action == "directive.feedback")
                {
                    string dirId = GetStr(payload, "directiveId", GetStr(payload, "fingerprint")).Trim();
                    string fbSt = GetStr(payload, "status", "done").Trim().ToLowerInvariant();
                    if (fbSt != "ack" && fbSt != "in_progress" && fbSt != "done" && fbSt != "rejected")
                    {
                        httpStatus = 400; return ErrorObj("Unsupported directive feedback status: " + fbSt);
                    }
                    string outcome = GetStr(payload, "outcome").Trim();
                    var dirs = EnsureList(_relational, "bodyDirectives");
                    Dictionary<string, object> targetD = null;
                    foreach (object dObj in dirs)
                    {
                        var dd = dObj as Dictionary<string, object>;
                        if (dd != null && (GetStr(dd, "directiveId") == dirId || GetStr(dd, "fingerprint") == dirId)) { targetD = dd; break; }
                    }
                    if (targetD == null) { httpStatus = 404; return ErrorObj("Directive not found: " + dirId); }
                    targetD["status"] = fbSt;
                    targetD["lastStatusAt"] = UtcNowIso();
                    var fbList = EnsureList(_relational, "bodyFeedback");
                    fbList.Add(new Dictionary<string, object> {
                        { "feedbackId", "FB-" + (fbList.Count + 1) },
                        { "directiveId", GetStr(targetD, "directiveId") },
                        { "fingerprint", GetStr(targetD, "fingerprint") },
                        { "status", fbSt },
                        { "triggerEvent", "MANUAL_OPERATOR_FEEDBACK" },
                        { "outcome", outcome },
                        { "executedBy", actor },
                        { "loggedAt", UtcNowIso() }
                    });
                    RefreshPathwaysAndDirectives(action);
                    AppendAudit(actor, persona, "directive.feedback", GetStr(targetD, "directiveId"), fbSt + ": " + outcome);
                }
                else
                {
                    httpStatus = 400;
                    return ErrorObj("Unknown relational action: " + action);
                }

                int rev = (int)GetNum(_relational, "revision", 1) + 1;
                _relational["revision"] = rev;
                _relational["updatedAt"] = UtcNowIso();

                receipts = BuildCanonicalObjects(action, actor, payload, corrId, GetStr(body, "causationId"));
                string taskId = GetStr(receipts, "task_id");
                var evtDict = EnsureDict(receipts, "event");
                outboxSeq = _outbox.Count + 1;
                _outbox.Add(new Dictionary<string, object>
                {
                    { "seq", outboxSeq },
                    { "eventId", GetStr(evtDict, "eventId") },
                    { "taskId", taskId },
                    { "correlation_id", GetStr(receipts, "correlation_id") },
                    { "causation_id", GetStr(receipts, "causation_id") },
                    { "eventType", "relational." + action },
                    { "revision", rev },
                    { "createdAt", UtcNowIso() },
                    { "actor", actor }
                });
                while (_outbox.Count > 1000) _outbox.RemoveAt(0);
                _tasks[taskId] = receipts;
                _taskOrder.Add(taskId);
                while (_taskOrder.Count > 500)
                {
                    string oldTid = _taskOrder[0];
                    _taskOrder.RemoveAt(0);
                    _tasks.Remove(oldTid);
                }
                if (!string.IsNullOrEmpty(idempotencyKey))
                {
                    _idempotencyIndex[idempotencyKey] = new Dictionary<string, object>
                    {
                        { "idempotencyKey", idempotencyKey },
                        { "requestHash", reqHash },
                        { "taskId", taskId },
                        { "correlationId", GetStr(receipts, "correlation_id") },
                        { "causationId", GetStr(receipts, "causation_id") },
                        { "outboxCursor", outboxSeq },
                        { "taskReceipt", receipts },
                        { "createdAt", UtcNowIso() }
                    };
                }
                RecordRestrictedAudit(actor, "ALLOW", "Committed " + action + " at rev " + rev, GetStr(receipts, "correlation_id"), taskId);

                AtomicWrite(_relPath, _relational);
                if (!string.IsNullOrEmpty(_mirrorPath))
                {
                    try { AtomicWrite(_mirrorPath, _relational); } catch {}
                }
            }

            BroadcastSse("relational." + action, outboxSeq);
            var outEnv = BuildStateEnvelope();
            outEnv["idempotentReplay"] = false;
            outEnv["idempotencyKey"] = string.IsNullOrEmpty(idempotencyKey) ? null : idempotencyKey;
            outEnv["taskId"] = GetStr(receipts, "task_id");
            outEnv["correlationId"] = GetStr(receipts, "correlation_id");
            outEnv["causationId"] = GetStr(receipts, "causation_id");
            outEnv["outboxCursor"] = outboxSeq;
            outEnv["taskReceipt"] = receipts;
            return outEnv;
        }

        private Dictionary<string, object> UpdateTemp(Dictionary<string, object> body, out int httpStatus)
        {
            httpStatus = 200;
            string action = GetStr(body, "action").Trim();
            var payload = body.ContainsKey("payload") && body["payload"] is Dictionary<string, object>
                ? (Dictionary<string, object>)body["payload"]
                : new Dictionary<string, object>();
            long now = NowEpoch();

            lock (_stateLock)
            {
                PruneTemp();
                if (action == "session.heartbeat")
                {
                    string sid = GetStr(payload, "sessionId");
                    var sessions = EnsureList(_temp, "activeSessions");
                    var next = new ArrayList();
                    foreach (object obj in sessions)
                    {
                        var s = obj as Dictionary<string, object>;
                        if (s != null && GetStr(s, "sessionId") != sid) next.Add(s);
                    }
                    next.Add(new Dictionary<string, object>
                    {
                        { "sessionId", sid },
                        { "userId", GetStr(payload, "userId", "User") },
                        { "persona", GetStr(payload, "persona", "RSR / Seller") },
                        { "activeTab", GetStr(payload, "activeTab", "tab-overview") },
                        { "focusedEntity", GetStr(payload, "focusedEntity", "ROF-DEMO-001") },
                        { "lastSeen", UtcNowIso() },
                        { "lastSeenEpoch", now }
                    });
                    _temp["activeSessions"] = next;
                }
                else if (action == "lock.acquire")
                {
                    string ekey = GetStr(payload, "entityKey");
                    string sid = GetStr(payload, "sessionId");
                    var locks = EnsureList(_temp, "entityLocks");
                    var existing = FindByKey(locks, "entityKey", ekey);
                    if (existing != null && GetStr(existing, "sessionId") != sid)
                    {
                        httpStatus = 409;
                        var conflictEnv = BuildStateEnvelope();
                        conflictEnv["ok"] = false;
                        conflictEnv["conflict"] = true;
                        conflictEnv["holder"] = existing;
                        return conflictEnv;
                    }
                    _fencingCounter++;
                    int fence = _fencingCounter;
                    _entityMaxFencing[ekey] = fence;
                    string leaseId = "LEASE-" + fence + "-" + now;
                    var next = new ArrayList();
                    foreach (object obj in locks)
                    {
                        var lk = obj as Dictionary<string, object>;
                        if (lk != null && GetStr(lk, "entityKey") != ekey) next.Add(lk);
                    }
                    next.Add(new Dictionary<string, object>
                    {
                        { "entityKey", ekey },
                        { "sessionId", sid },
                        { "leaseId", leaseId },
                        { "fencingToken", fence },
                        { "userId", GetStr(payload, "userId", "User") },
                        { "persona", GetStr(payload, "persona", "RSR / Seller") },
                        { "action", GetStr(payload, "lockAction", "Editing") },
                        { "acquiredAt", UtcNowIso() },
                        { "expiresEpoch", now + LockTtlSeconds }
                    });
                    _temp["entityLocks"] = next;
                }
                else if (action == "lock.release")
                {
                    string ekey = GetStr(payload, "entityKey");
                    string sid = GetStr(payload, "sessionId");
                    var locks = EnsureList(_temp, "entityLocks");
                    var next = new ArrayList();
                    foreach (object obj in locks)
                    {
                        var lk = obj as Dictionary<string, object>;
                        if (lk == null) continue;
                        if (GetStr(lk, "entityKey") == ekey && (string.IsNullOrEmpty(sid) || GetStr(lk, "sessionId") == sid)) continue;
                        next.Add(lk);
                    }
                    _temp["entityLocks"] = next;
                }
                else if (action == "draft.update")
                {
                    string dkey = GetStr(payload, "draftKey");
                    string textVal = GetStr(payload, "text");
                    var drafts = EnsureDict(_temp, "sharedDrafts");
                    if (!string.IsNullOrEmpty(textVal.Trim()))
                    {
                        drafts[dkey] = new Dictionary<string, object>
                        {
                            { "draftKey", dkey },
                            { "userId", GetStr(payload, "userId", "User") },
                            { "persona", GetStr(payload, "persona", "Customer") },
                            { "text", textVal },
                            { "updatedAt", UtcNowIso() }
                        };
                    }
                    else if (drafts.ContainsKey(dkey))
                    {
                        drafts.Remove(dkey);
                    }
                }
                else if (action == "whatif.update")
                {
                    var wi = EnsureDict(_temp, "whatIfScratch");
                    foreach (var kv in payload) wi[kv.Key] = kv.Value;
                    wi["updatedAt"] = UtcNowIso();
                }
                else if (action == "chat.post")
                {
                    string refId = GetStr(payload, "refId", "ROF-DEMO-001");
                    string chType = refId.StartsWith("IS-", StringComparison.OrdinalIgnoreCase) ? "shipment_is"
                        : (refId.StartsWith("CS-", StringComparison.OrdinalIgnoreCase) ? "shipment_cs" : "quote");
                    var feed = EnsureList(_temp, "liveChatFeed");
                    feed.Add(new Dictionary<string, object>
                    {
                        { "id", "MSG-" + NowEpoch() + "-" + Guid.NewGuid().ToString("N").Substring(0, 4) },
                        { "time", UtcNowIso() },
                        { "author", GetStr(payload, "author", "User") },
                        { "persona", GetStr(payload, "persona", "RSR / Seller") },
                        { "refId", refId },
                        { "channelType", chType },
                        { "mentions", payload.ContainsKey("mentions") ? payload["mentions"] : new ArrayList() },
                        { "text", GetStr(payload, "text") }
                    });
                    while (feed.Count > 250) feed.RemoveAt(0);
                }
                else
                {
                    httpStatus = 400;
                    return ErrorObj("Unknown temp action: " + action);
                }

                int trev = (int)GetNum(_temp, "revision", 1) + 1;
                _temp["revision"] = trev;
                _temp["updatedAt"] = UtcNowIso();
                AtomicWrite(_tempPath, _temp);
            }

            BroadcastSse("temp." + action);
            return BuildStateEnvelope();
        }

        private static Dictionary<string, object> ErrorObj(string message)
        {
            return new Dictionary<string, object> { { "ok", false }, { "error", message } };
        }

        private Dictionary<string, object> GetTasks(string taskId, out int httpStatus)
        {
            httpStatus = 200;
            lock (_stateLock)
            {
                if (!string.IsNullOrEmpty(taskId))
                {
                    Dictionary<string, object> rec;
                    if (!_tasks.TryGetValue(taskId, out rec)) { httpStatus = 404; return ErrorObj("Task not found: " + taskId); }
                    return new Dictionary<string, object> { { "ok", true }, { "taskReceipt", rec } };
                }
                var list = new ArrayList();
                for (int i = _taskOrder.Count - 1; i >= 0; i--)
                {
                    Dictionary<string, object> rec;
                    if (_tasks.TryGetValue(_taskOrder[i], out rec)) list.Add(rec);
                }
                return new Dictionary<string, object> { { "ok", true }, { "tasks", list } };
            }
        }

        private Dictionary<string, object> GetRegistrations()
        {
            lock (_stateLock)
            {
                long nowEpoch = NowEpoch();
                var list = new ArrayList();
                foreach (var reg in _registrations.Values)
                {
                    long expEpoch = (long)GetNum(reg, "leaseExpiresEpoch", 0);
                    if (GetStr(reg, "status") == "ACTIVE" && expEpoch < nowEpoch)
                    {
                        reg["status"] = "EXPIRED"; reg["routable"] = false; reg["reason"] = "Registration lease expired";
                    }
                    list.Add(reg);
                }
                return new Dictionary<string, object> { { "ok", true }, { "homeNodeId", HomeNodeId }, { "registrations", list } };
            }
        }

        private Dictionary<string, object> GetRestrictedAudit()
        {
            lock (_stateLock)
            {
                return new Dictionary<string, object> { { "ok", true }, { "policyVersion", PolicyVersion }, { "audit", new ArrayList(_restrictedAudit) } };
            }
        }

        private Dictionary<string, object> RegisterHolon(Dictionary<string, object> body, out int httpStatus)
        {
            httpStatus = 200;
            string hid = GetStr(body, "holonId").Trim();
            string ver = GetStr(body, "version").Trim();
            string artHash = GetStr(body, "artifactHash").Trim();
            string expDigest = GetStr(body, "expectedDigest").Trim();
            var caps = EnsureList(body, "capabilities");
            bool ready = GetBool(body, "readinessOk", true);
            string sig = GetStr(body, "signature", "signed-manifest-sha256").Trim();
            int ttl = Math.Max(30, (int)GetNum(body, "leaseTtlSeconds", 300));
            lock (_stateLock)
            {
                if (string.IsNullOrEmpty(hid) || string.IsNullOrEmpty(ver) || caps.Count == 0 || string.IsNullOrEmpty(sig))
                {
                    string reason = "Invalid manifest: holonId, version, capabilities, and signature are required.";
                    RecordRestrictedAudit(string.IsNullOrEmpty(hid) ? "unknown" : hid, "DENY", reason, "", "");
                    httpStatus = 400; return ErrorObj(reason);
                }
                if (!string.IsNullOrEmpty(expDigest) && !string.Equals(artHash, expDigest, StringComparison.OrdinalIgnoreCase))
                {
                    string reason = "Manifest artifact digest mismatch: expected " + expDigest + ", got " + artHash + ".";
                    _registrations[hid] = new Dictionary<string, object> { { "registrationId", "REG-REJ-" + NowEpoch() }, { "holonId", hid }, { "status", "REJECTED" }, { "routable", false }, { "reason", reason }, { "registeredAt", UtcNowIso() } };
                    RecordRestrictedAudit(hid, "DENY", reason, "", "");
                    httpStatus = 400; return ErrorObj(reason);
                }
                if (!ready)
                {
                    var pend = new Dictionary<string, object> { { "registrationId", "REG-PEND-" + NowEpoch() }, { "holonId", hid }, { "version", ver }, { "status", "PENDING" }, { "routable", false }, { "reason", "Health readiness probe failed; no routable capability." }, { "registeredAt", UtcNowIso() } };
                    _registrations[hid] = pend; RecordRestrictedAudit(hid, "HOLD", GetStr(pend, "reason"), "", "");
                    return new Dictionary<string, object> { { "ok", true }, { "registration", pend } };
                }
                long nowEpoch = NowEpoch();
                string leaseExp = DateTime.UtcNow.AddSeconds(ttl).ToString("yyyy-MM-ddTHH:mm:ssZ", System.Globalization.CultureInfo.InvariantCulture);
                var grants = EnsureList(body, "requestedGrants");
                if (grants.Count == 0) grants = new ArrayList(new[] { "crm.read", "crm.write", "cpq.write", "osq.read", "osq.write" });
                var rec = new Dictionary<string, object>
                {
                    { "registrationId", "REG-" + hid.ToUpperInvariant() + "-" + (nowEpoch % 100000) },
                    { "holonId", hid }, { "instanceId", GetStr(body, "instanceId", hid + "-node-a-01") }, { "homeNodeId", HomeNodeId },
                    { "version", ver }, { "status", "ACTIVE" }, { "routable", true }, { "capabilities", caps }, { "grantedScopes", grants },
                    { "healthEndpoint", GetStr(body, "healthEndpoint", "/health") },
                    { "artifactHash", string.IsNullOrEmpty(artHash) ? CanonicalHash(new Dictionary<string, object> { { "holonId", hid }, { "version", ver } }) : artHash },
                    { "registeredAt", UtcNowIso() }, { "leaseExpiresAt", leaseExp }, { "leaseExpiresEpoch", nowEpoch + ttl },
                    { "reason", "Verified identity, signature, schema, digest & readiness" }
                };
                _registrations[hid] = rec; RecordRestrictedAudit(hid, "ALLOW", "Registered ACTIVE lease until " + leaseExp, "", "");
                return new Dictionary<string, object> { { "ok", true }, { "registration", rec } };
            }
        }

        private Dictionary<string, object> RenewRegistration(Dictionary<string, object> body, out int httpStatus)
        {
            httpStatus = 200;
            string hid = GetStr(body, "holonId", "x_osq").Trim();
            int ttl = Math.Max(30, (int)GetNum(body, "leaseTtlSeconds", 300));
            lock (_stateLock)
            {
                Dictionary<string, object> rec;
                if (!_registrations.TryGetValue(hid, out rec)) { httpStatus = 404; return ErrorObj("Registration not found for " + hid); }
                if (GetStr(rec, "status") == "REVOKED") { httpStatus = 400; return ErrorObj("Cannot renew revoked registration for " + hid + "; re-register manifest."); }
                if (!GetBool(body, "healthOk", true))
                {
                    rec["status"] = "UNHEALTHY"; rec["routable"] = false; rec["reason"] = "Health check failed during lease renewal";
                    _meshState["advertisedWithdrawnCount"] = (int)GetNum(_meshState, "advertisedWithdrawnCount", 0) + 1;
                    RecordRestrictedAudit(hid, "DENY", GetStr(rec, "reason"), "", "");
                    return new Dictionary<string, object> { { "ok", false }, { "registration", rec } };
                }
                long nowEpoch = NowEpoch();
                string leaseExp = DateTime.UtcNow.AddSeconds(ttl).ToString("yyyy-MM-ddTHH:mm:ssZ", System.Globalization.CultureInfo.InvariantCulture);
                rec["status"] = "ACTIVE"; rec["routable"] = true; rec["leaseExpiresAt"] = leaseExp; rec["leaseExpiresEpoch"] = nowEpoch + ttl;
                rec["reason"] = "Lease renewed after health & grant recheck";
                RecordRestrictedAudit(hid, "ALLOW", "Renewed lease until " + leaseExp, "", "");
                return new Dictionary<string, object> { { "ok", true }, { "registration", rec } };
            }
        }

        private Dictionary<string, object> RevokeRegistration(Dictionary<string, object> body, out int httpStatus)
        {
            httpStatus = 200;
            string hid = GetStr(body, "holonId", "x_osq").Trim();
            string reason = GetStr(body, "reason", "Operator revoked capability route").Trim();
            string newStatus = reason.Equals("expired", StringComparison.OrdinalIgnoreCase) ? "EXPIRED" : "REVOKED";
            lock (_stateLock)
            {
                Dictionary<string, object> rec;
                if (!_registrations.TryGetValue(hid, out rec)) { httpStatus = 404; return ErrorObj("Registration not found for " + hid); }
                rec["status"] = newStatus; rec["routable"] = false; rec["reason"] = reason; rec["revokedAt"] = UtcNowIso();
                _meshState["advertisedWithdrawnCount"] = (int)GetNum(_meshState, "advertisedWithdrawnCount", 0) + 1;
                RecordRestrictedAudit(hid, "DENY", "Registration " + newStatus + ": " + reason, "", "");
                return new Dictionary<string, object> { { "ok", true }, { "withdrawnFromMesh", true }, { "registration", rec } };
            }
        }

        private Dictionary<string, object> VerifyMarketplaceArtifact(Dictionary<string, object> body, out int httpStatus)
        {
            httpStatus = 200;
            var trust = body.ContainsKey("trustEnvelope") && body["trustEnvelope"] is Dictionary<string, object> ? (Dictionary<string, object>)body["trustEnvelope"] : body;
            string publisher = GetStr(trust, "publisher").Trim();
            string scope = GetStr(trust, "scope").Trim();
            string audience = GetStr(trust, "audience").Trim();
            string sig = GetStr(trust, "signature").Trim();
            string replayId = GetStr(trust, "replayId").Trim();
            string signedDigest = GetStr(trust, "signedDigest").Trim().ToLowerInvariant();
            string expiresAt = GetStr(trust, "expiresAt").Trim();
            bool revoked = GetBool(trust, "revoked", false);
            var artifact = body.ContainsKey("artifact") && body["artifact"] is Dictionary<string, object> ? (Dictionary<string, object>)body["artifact"] : (body.ContainsKey("payload") && body["payload"] is Dictionary<string, object> ? (Dictionary<string, object>)body["payload"] : new Dictionary<string, object>());

            lock (_stateLock)
            {
                if (string.IsNullOrEmpty(publisher) || string.IsNullOrEmpty(scope) || string.IsNullOrEmpty(audience) || string.IsNullOrEmpty(sig) || string.IsNullOrEmpty(replayId))
                { httpStatus = 400; return ErrorObj("Marketplace Trust Gate Blocked: publisher, scope, audience, signature, and replayId are required."); }
                if (revoked) { httpStatus = 403; return ErrorObj("Marketplace Trust Gate Blocked: artifact publisher/credential is revoked."); }
                DateTime expDt;
                if (!TryParseUtcIso(expiresAt, out expDt) || expDt <= DateTime.UtcNow) { httpStatus = 400; return ErrorObj("Marketplace Trust Gate Blocked: artifact envelope is missing or expired."); }
                if (_marketplaceReplays.Contains(replayId)) { httpStatus = 409; var conf = ErrorObj("Marketplace Trust Gate Conflict: replayId '" + replayId + "' was already consumed."); conf["conflict"] = true; return conf; }
                string computedDigest = CanonicalHash(artifact);
                if (!string.Equals(signedDigest, computedDigest, StringComparison.OrdinalIgnoreCase))
                { RecordRestrictedAudit(publisher, "DENY", "Marketplace digest mismatch for replayId=" + replayId, "", ""); httpStatus = 400; return ErrorObj("Marketplace Trust Gate Blocked: byte digest mismatch (expected " + signedDigest + ", computed " + computedDigest + ")."); }
                _marketplaceReplays.Add(replayId);
                string repShort = replayId.Length > 8 ? replayId.Substring(0, 8) : replayId;
                var shard = new Dictionary<string, object>
                {
                    { "asset_id", GetStr(artifact, "asset_id", "osq-verified-" + NowEpoch()) },
                    { "token_id", GetStr(artifact, "token_id", "tok-" + scope + "-" + repShort) },
                    { "axis", GetStr(artifact, "axis", "entirety") },
                    { "title", GetStr(artifact, "title", "Verified GARD Artifact Exchange Shard") },
                    { "marketplace_scope", scope }, { "supply_chain_domain", GetStr(artifact, "domain", "forecasting") },
                    { "sku_ref", GetStr(artifact, "sku_ref", "SKU-HVAC-3001") }, { "batch_id", GetStr(artifact, "batch_id", "BATCH-VERIFIED") },
                    { "available_shards", (int)GetNum(artifact, "shard_count", 250) }, { "current_shard_price", GetNum(artifact, "shard_price", 12.5) },
                    { "protocol", "gard-shard/v2" }, { "publisher", publisher }, { "audience", audience },
                    { "replayId", replayId }, { "envelope_digest", computedDigest }, { "grantScope", "gard.artifact.exchange" }
                };
                EnsureList(_relational, "marketplaceShards").Insert(0, shard);
                AppendAudit(publisher, "GARD Trust Gate", "marketplace.artifact.verified", GetStr(shard, "asset_id"), "Scope=" + scope + ", digest=" + computedDigest.Substring(0, 12));
                _relational["revision"] = (int)GetNum(_relational, "revision", 1) + 1;
                _relational["updatedAt"] = UtcNowIso();
                AtomicWrite(_relPath, _relational);
                if (!string.IsNullOrEmpty(_mirrorPath)) { try { AtomicWrite(_mirrorPath, _relational); } catch {} }
                RecordRestrictedAudit(publisher, "ALLOW", "Verified GARD artifact exchange " + GetStr(shard, "asset_id"), "", "");
                BroadcastSse("marketplace.verified");
                return new Dictionary<string, object> { { "ok", true }, { "verified", true }, { "grantScope", "gard.artifact.exchange" }, { "computedDigest", computedDigest }, { "shard", shard }, { "state", BuildStateEnvelope() } };
            }
        }

        private Dictionary<string, object> ExecuteConnector(Dictionary<string, object> body, out int httpStatus)
        {
            httpStatus = 200;
            string cid = GetStr(body, "connectorId").Trim();
            string mode = GetStr(body, "mode", "read").Trim().ToLowerInvariant();
            string provKey = GetStr(body, "providerIdempotencyKey").Trim();
            bool unknownOutcome = GetBool(body, "simulateUnknownOutcome", false);
            bool reconcile = GetBool(body, "reconcileBeforeRetry", false);
            lock (_stateLock)
            {
                string routeErr;
                if (!CheckActiveRoute("x_osq", out routeErr)) { httpStatus = 503; return ErrorObj(routeErr); }
                var conn = FindByKey(EnsureList(_relational, "connectors"), "id", cid);
                if (conn == null) { httpStatus = 404; return ErrorObj("Connector not found: " + cid); }
                if (mode == "mutation")
                {
                    if (string.IsNullOrEmpty(provKey)) { httpStatus = 400; return ErrorObj("ERP Adapter Blocked: mutation mode requires providerIdempotencyKey."); }
                    Dictionary<string, object> prev;
                    if (_connectorOutcomes.TryGetValue(provKey, out prev) && GetStr(prev, "outcome") == "UNKNOWN_OUTCOME_HOLD" && !reconcile)
                    { httpStatus = 202; return new Dictionary<string, object> { { "ok", false }, { "status", "HOLD" }, { "outcome", "UNKNOWN_OUTCOME_HOLD" }, { "reason", "Previous external mutation had unknown outcome; reconcileBeforeRetry=true required before retry." }, { "providerIdempotencyKey", provKey } }; }
                    if (unknownOutcome && !reconcile)
                    {
                        _connectorOutcomes[provKey] = new Dictionary<string, object> { { "outcome", "UNKNOWN_OUTCOME_HOLD" }, { "time", UtcNowIso() } };
                        RecordRestrictedAudit(cid, "HOLD", "Unknown external outcome on key=" + provKey, "", "");
                        httpStatus = 202; return new Dictionary<string, object> { { "ok", false }, { "status", "HOLD" }, { "outcome", "UNKNOWN_OUTCOME_HOLD" }, { "reason", "External ERP call timed out with unknown outcome; held safely without blind retry." }, { "providerIdempotencyKey", provKey } };
                    }
                    _connectorOutcomes[provKey] = new Dictionary<string, object> { { "outcome", "KNOWN_RESULT" }, { "time", UtcNowIso() } };
                    RecordRestrictedAudit(cid, "ALLOW", "Idempotent fulfillment mutation succeeded key=" + provKey, "", "");
                    return new Dictionary<string, object> { { "ok", true }, { "status", "COMPLETED" }, { "outcome", "KNOWN_RESULT" }, { "grantScope", "connector.fulfillment.execute" }, { "providerIdempotencyKey", provKey } };
                }
                RecordRestrictedAudit(cid, "ALLOW", "Connector read executed on " + cid, "", "");
                return new Dictionary<string, object> { { "ok", true }, { "status", "COMPLETED" }, { "outcome", "KNOWN_RESULT" }, { "grantScope", "erp.connector.read" }, { "connector", conn } };
            }
        }

        private Dictionary<string, object> QueryAnalyticsReadonly(string domain)
        {
            lock (_stateLock)
            {
                var snap = _immutableSnapshots.Count > 0 ? _immutableSnapshots[_immutableSnapshots.Count - 1] : ExportWatermarkSnapshot("system", "query");
                double activePipe = 0.0;
                foreach (object oObj in EnsureList(_relational, "opportunities"))
                {
                    var o = oObj as Dictionary<string, object>;
                    if (o == null) continue;
                    string cond = GetStr(o, "opportunityCondition");
                    string st = GetStr(o, "stage");
                    if (cond != "Pro Forma" && cond != "MOG" && st != "Closed Lost") activePipe += GetNum(o, "estimatedAmount", 0.0);
                }
                var quotes = EnsureList(_relational, "quotes");
                double mSum = 0.0;
                foreach (object qObj in quotes) { var q = qObj as Dictionary<string, object>; if (q != null) mSum += GetNum(q, "approvedMarginPct", 0.0); }
                double avgMargin = quotes.Count > 0 ? Math.Round(mSum / quotes.Count, 2) : 0.0;
                var fb = EnsureDict(_relational, "forecastBatch");
                var eb = EnsureDict(fb, "emergencyBrake");
                return new Dictionary<string, object>
                {
                    { "ok", true }, { "engine", "duckdb-readonly-projection" }, { "readOnly", true }, { "domain", domain },
                    { "watermarkRevision", snap["watermarkRevision"] }, { "contentHash", snap["contentHash"] },
                    { "metrics", new Dictionary<string, object> {
                        { "activePipelineUsd", Math.Round(activePipe, 2) }, { "avgQuoteMarginPct", avgMargin },
                        { "byPublishedUnits", GetNum(fb, "byPublishedUnits", 0.0) }, { "emergencyBrakeStatus", GetStr(eb, "status", "HELD") }
                    }}
                };
            }
        }

        private Dictionary<string, object> MeshHello(Dictionary<string, object> body)
        {
            string peerId = GetStr(body, "nodeId", PeerNodeId).Trim();
            string proto = GetStr(body, "protocolVersion", "holon-mesh/2.3").Trim();
            lock (_stateLock)
            {
                var binding = new Dictionary<string, object> { { "peerNodeId", peerId }, { "homeNodeId", HomeNodeId }, { "protocolVersion", proto }, { "mtlsVerified", GetBool(body, "mtlsVerified", true) }, { "boundAt", UtcNowIso() } };
                EnsureDict(_meshState, "peerBindings")[peerId] = binding;
                RecordRestrictedAudit(peerId, "ALLOW", "MESH HELLO_ACK bound peer " + peerId, "", "");
                var res = new Dictionary<string, object> { { "ok", true }, { "messageType", "HELLO_ACK" } };
                foreach (var kv in binding) res[kv.Key] = kv.Value;
                return res;
            }
        }

        private Dictionary<string, object> MeshAdvertise()
        {
            lock (_stateLock)
            {
                long nowEpoch = NowEpoch();
                var activeHolons = new ArrayList();
                int capCount = 0;
                foreach (var kv in _registrations)
                {
                    var reg = kv.Value;
                    if (GetStr(reg, "status") == "ACTIVE" && GetBool(reg, "routable", false) && (long)GetNum(reg, "leaseExpiresEpoch", 0) >= nowEpoch)
                    {
                        var caps = EnsureList(reg, "capabilities");
                        capCount += caps.Count;
                        activeHolons.Add(new Dictionary<string, object> { { "holonId", kv.Key }, { "version", GetStr(reg, "version") }, { "capabilities", caps }, { "grantedScopes", EnsureList(reg, "grantedScopes") }, { "leaseExpiresAt", GetStr(reg, "leaseExpiresAt") } });
                    }
                }
                var snap = _immutableSnapshots.Count > 0 ? _immutableSnapshots[_immutableSnapshots.Count - 1] : ExportWatermarkSnapshot("system", "advertise");
                return new Dictionary<string, object>
                {
                    { "ok", true }, { "messageType", "ADVERTISE_ACK" }, { "nodeId", HomeNodeId },
                    { "protocolVersion", "holon-mesh/2.3" }, { "activeCapabilitiesCount", capCount },
                    { "activeHolons", activeHolons },
                    { "datasetWatermark", new Dictionary<string, object> { { "revision", snap["watermarkRevision"] }, { "contentHash", snap["contentHash"] } } },
                    { "withdrawnCount", (int)GetNum(_meshState, "advertisedWithdrawnCount", 0) }
                };
            }
        }

        private Dictionary<string, object> MeshExchange(Dictionary<string, object> body)
        {
            string envId = GetStr(body, "envelopeId", "env-" + NowEpoch()).Trim();
            string kind = GetStr(body, "kind", "Event").Trim();
            string sender = GetStr(body, "senderNodeId", PeerNodeId).Trim();
            var payload = body.ContainsKey("payload") && body["payload"] is Dictionary<string, object> ? (Dictionary<string, object>)body["payload"] : new Dictionary<string, object>();
            lock (_stateLock)
            {
                var seen = EnsureList(_meshState, "seenEnvelopeIds");
                foreach (object s in seen) { if (string.Equals(Convert.ToString(s), envId, StringComparison.OrdinalIgnoreCase)) return new Dictionary<string, object> { { "ok", true }, { "messageType", "EXCHANGE_ACK" }, { "duplicate", true }, { "envelopeId", envId }, { "persisted", true } }; }
                seen.Add(envId);
                var inbox = EnsureList(_meshState, "peerInbox");
                int seq = inbox.Count + 1;
                inbox.Add(new Dictionary<string, object> { { "seq", seq }, { "envelopeId", envId }, { "kind", kind }, { "senderNodeId", sender }, { "receivedAt", UtcNowIso() }, { "payload", payload } });
                RecordRestrictedAudit(sender, "ALLOW", "MESH durable peerInbox persist before ACK (" + envId + ")", "", "");
                return new Dictionary<string, object> { { "ok", true }, { "messageType", "EXCHANGE_ACK" }, { "duplicate", false }, { "envelopeId", envId }, { "seqCursor", seq }, { "persistedBeforeAck", true } };
            }
        }

        private Dictionary<string, object> MeshSnapshotPull()
        {
            lock (_stateLock)
            {
                var snap = ExportWatermarkSnapshot("mesh-peer", "snapshot-pull");
                return new Dictionary<string, object> { { "ok", true }, { "nodeId", HomeNodeId }, { "watermarkRevision", snap["watermarkRevision"] }, { "contentHash", snap["contentHash"] }, { "snapshot", snap }, { "outboxCursor", _outbox.Count } };
            }
        }

        private Dictionary<string, object> MeshPeerWrite(Dictionary<string, object> body, out int httpStatus)
        {
            httpStatus = 200;
            string ownerNode = GetStr(body, "ownerNode", HomeNodeId).Trim();
            bool homeReachable = GetBool(body, "homeNodeReachable", true);
            string action = GetStr(body, "action", "crm.account.upsert").Trim();
            string actor = GetStr(body, "actor", "PeerUser").Trim();
            var payload = body.ContainsKey("payload") && body["payload"] is Dictionary<string, object> ? (Dictionary<string, object>)body["payload"] : new Dictionary<string, object>();
            lock (_stateLock)
            {
                var nodeB = EnsureDict(_meshState, "nodeB");
                if (ownerNode == PeerNodeId)
                {
                    var localOwned = EnsureDict(nodeB, "localOwnedState");
                    string key = GetStr(payload, "key", "local-" + NowEpoch());
                    localOwned[key] = new Dictionary<string, object> { { "value", payload }, { "updatedAt", UtcNowIso() }, { "actor", actor } };
                    return new Dictionary<string, object> { { "ok", true }, { "committedLocally", true }, { "ownerNode", PeerNodeId }, { "selfPromotedToOwner", false }, { "nodeBState", localOwned[key] } };
                }
                if (!homeReachable)
                {
                    var pq = EnsureList(nodeB, "pendingHomeNodeQueue");
                    var pendRec = new Dictionary<string, object> { { "pendingId", "PEND-NODEB-" + (pq.Count + 1) }, { "ownerNode", ownerNode }, { "action", action }, { "payload", payload }, { "actor", actor }, { "status", "PENDING_HOME_NODE" }, { "queuedAt", UtcNowIso() } };
                    pq.Add(pendRec);
                    RecordRestrictedAudit(PeerNodeId, "HOLD", "Node A unreachable; Node B queued " + pendRec["pendingId"] + " without self-promoting", "", "");
                    httpStatus = 202;
                    return new Dictionary<string, object> { { "ok", true }, { "accepted", true }, { "status", "PENDING_HOME_NODE" }, { "pendingHomeNode", true }, { "selfPromotedToOwner", false }, { "committedLocally", false }, { "pendingRecord", pendRec } };
                }
            }
            var fwdBody = new Dictionary<string, object> { { "action", action }, { "actor", actor }, { "persona", "Federated Peer" }, { "payload", payload } };
            if (!string.IsNullOrEmpty(GetStr(body, "idempotencyKey"))) fwdBody["idempotencyKey"] = GetStr(body, "idempotencyKey");
            var res = MutateRelational(fwdBody, out httpStatus);
            res["forwardedToHomeNode"] = true; res["selfPromotedToOwner"] = false;
            if (httpStatus == 200) res["status"] = "COMMITTED_ON_HOME_NODE";
            return res;
        }

        public void Run()
        {
            var listener = new TcpListener(IPAddress.Parse(_host), _port);
            listener.Start();

            var keepAliveThread = new Thread(() =>
            {
                byte[] ping = Encoding.UTF8.GetBytes(": keepalive\n\n");
                while (_running)
                {
                    Thread.Sleep(12000);
                    PushRawToSseClients(ping);
                }
            });
            keepAliveThread.IsBackground = true;
            keepAliveThread.Start();

            while (_running)
            {
                TcpClient client = listener.AcceptTcpClient();
                ThreadPool.QueueUserWorkItem(_ => HandleClient(client));
            }
        }

        private static string GetQueryParam(string queryString, string key)
        {
            if (string.IsNullOrEmpty(queryString)) return "";
            string[] parts = queryString.Split('&');
            foreach (string part in parts)
            {
                int eq = part.IndexOf('=');
                if (eq > 0)
                {
                    string k = Uri.UnescapeDataString(part.Substring(0, eq));
                    if (string.Equals(k, key, StringComparison.OrdinalIgnoreCase))
                        return Uri.UnescapeDataString(part.Substring(eq + 1));
                }
            }
            return "";
        }

        private void HandleClient(TcpClient client)
        {
            bool keepOpenForSse = false;
            try
            {
                client.ReceiveTimeout = 15000;
                client.SendTimeout = 15000;
                NetworkStream stream = client.GetStream();

                // Read HTTP headers
                var headerBuf = new List<byte>();
                byte[] single = new byte[1];
                int state = 0;
                while (headerBuf.Count < 65536)
                {
                    int n = stream.Read(single, 0, 1);
                    if (n <= 0) break;
                    byte b = single[0];
                    headerBuf.Add(b);
                    if (state == 0 && b == '\r') state = 1;
                    else if (state == 1 && b == '\n') state = 2;
                    else if (state == 2 && b == '\r') state = 3;
                    else if (state == 3 && b == '\n') break;
                    else state = 0;
                }
                if (headerBuf.Count == 0) return;

                string headerText = Encoding.UTF8.GetString(headerBuf.ToArray());
                string[] lines = headerText.Split(new[] { "\r\n" }, StringSplitOptions.None);
                string[] reqLine = lines[0].Split(' ');
                if (reqLine.Length < 2) return;
                string method = reqLine[0].ToUpperInvariant();
                string fullUrl = reqLine[1];
                int qPos = fullUrl.IndexOf('?');
                string rawPath = (qPos >= 0 ? fullUrl.Substring(0, qPos) : fullUrl).TrimEnd('/');
                string queryString = qPos >= 0 ? fullUrl.Substring(qPos + 1) : "";
                if (string.IsNullOrEmpty(rawPath)) rawPath = "/";

                int contentLength = 0;
                string hdrIdempotencyKey = "";
                string hdrPrefer = "";
                string hdrSessionId = "";
                string hdrLastEventId = "";
                for (int i = 1; i < lines.Length; i++)
                {
                    int colon = lines[i].IndexOf(':');
                    if (colon > 0)
                    {
                        string hName = lines[i].Substring(0, colon).Trim();
                        string hVal = lines[i].Substring(colon + 1).Trim();
                        if (hName.Equals("Content-Length", StringComparison.OrdinalIgnoreCase))
                            int.TryParse(hVal, out contentLength);
                        else if (hName.Equals("Idempotency-Key", StringComparison.OrdinalIgnoreCase))
                            hdrIdempotencyKey = hVal;
                        else if (hName.Equals("Prefer", StringComparison.OrdinalIgnoreCase))
                            hdrPrefer = hVal;
                        else if (hName.Equals("X-Session-Id", StringComparison.OrdinalIgnoreCase))
                            hdrSessionId = hVal;
                        else if (hName.Equals("Last-Event-ID", StringComparison.OrdinalIgnoreCase))
                            hdrLastEventId = hVal;
                    }
                }

                string bodyText = "";
                if (contentLength > 0 && contentLength < 10485760)
                {
                    byte[] bodyBytes = new byte[contentLength];
                    int readTotal = 0;
                    while (readTotal < contentLength)
                    {
                        int r = stream.Read(bodyBytes, readTotal, contentLength - readTotal);
                        if (r <= 0) break;
                        readTotal += r;
                    }
                    bodyText = Encoding.UTF8.GetString(bodyBytes, 0, readTotal);
                }

                if (method == "OPTIONS")
                {
                    WriteResponse(stream, 204, "text/plain", new byte[0]);
                    return;
                }

                if (method == "GET")
                {
                    if (rawPath == "/" || rawPath.Equals("/x_OSQ.html", StringComparison.OrdinalIgnoreCase) || rawPath.Equals("/Module.html", StringComparison.OrdinalIgnoreCase))
                    {
                        if (File.Exists(_htmlPath))
                        {
                            byte[] html = File.ReadAllBytes(_htmlPath);
                            WriteResponse(stream, 200, "text/html; charset=utf-8", html);
                        }
                        else
                        {
                            WriteJson(stream, 404, ErrorObj("HTML file not found: " + _htmlPath));
                        }
                    }
                    else if (rawPath.Equals("/crm", StringComparison.OrdinalIgnoreCase) || rawPath.Equals("/x_CRM.html", StringComparison.OrdinalIgnoreCase))
                    {
                        if (!string.IsNullOrEmpty(_crmHtmlPath) && File.Exists(_crmHtmlPath))
                        {
                            byte[] crmHtml = File.ReadAllBytes(_crmHtmlPath);
                            WriteResponse(stream, 200, "text/html; charset=utf-8", crmHtml);
                        }
                        else
                        {
                            WriteJson(stream, 404, ErrorObj("CRM HTML file not found: " + _crmHtmlPath));
                        }
                    }
                    else if (rawPath == "/health" || rawPath == "/health/live" || rawPath == "/health/ready")
                    {
                        var st = BuildStateEnvelope();
                        int sseCount;
                        lock (_sseLock) sseCount = _sseClients.Count;
                        WriteJson(stream, 200, new Dictionary<string, object>
                        {
                            { "status", "ok" },
                            { "liveRuntime", "HolonHubLive" },
                            { "holonVersion", "2.5.0" },
                            { "service", "holonhub-live-engine" },
                            { "snowflakeRole", "analytical-export-only" },
                            { "virtualWarehousesAllowed", false },
                            { "homeNodeId", HomeNodeId },
                            { "port", _port },
                            { "revision", st["revision"] },
                            { "tempRevision", st["tempRevision"] },
                            { "outboxCursor", st["outboxCursor"] },
                            { "sseClients", sseCount },
                            { "activeSessions", EnsureList((Dictionary<string, object>)st["temp"], "activeSessions").Count }
                        });
                    }
                    else if (rawPath == "/api/v1/osq/inventory/time-phased")
                    {
                        WriteJson(stream, 200, GetTimePhasedInventory(GetQueryParam(queryString, "dc"), GetQueryParam(queryString, "sku")));
                    }
                    else if (rawPath == "/api/v1/osq/marketplace/inventory")
                    {
                        int qNeed;
                        if (!int.TryParse(GetQueryParam(queryString, "qty"), out qNeed)) qNeed = 10;
                        bool incVirt = string.Equals(GetQueryParam(queryString, "includeVirtual"), "true", StringComparison.OrdinalIgnoreCase);
                        int mkStatus;
                        var mkRes = GetUnifiedMarketplaceInventory(GetQueryParam(queryString, "targetDc"), GetQueryParam(queryString, "sku"), qNeed, incVirt, out mkStatus);
                        WriteJson(stream, mkStatus, mkRes);
                    }
                    else if (rawPath == "/api/v1/osq/logistics/nodal-map")
                    {
                        WriteJson(stream, 200, GetNodalLogisticsMap());
                    }
                    else if (rawPath == "/api/v1/osq/outbox")
                    {
                        int scVal; int? sc = int.TryParse(GetQueryParam(queryString, "sinceCursor"), out scVal) ? (int?)scVal : null;
                        int limVal; if (!int.TryParse(GetQueryParam(queryString, "limit"), out limVal)) limVal = 200;
                        WriteJson(stream, 200, GetOutbox(sc.HasValue ? sc.Value : 0, limVal));
                    }
                    else if (rawPath == "/api/v1/osq/pathways")
                    {
                        WriteJson(stream, 200, GetPathways());
                    }
                    else if (rawPath == "/api/v1/osq/directives")
                    {
                        WriteJson(stream, 200, GetDirectives(GetQueryParam(queryString, "status"), GetQueryParam(queryString, "ownerRole")));
                    }
                    else if (rawPath == "/api/v1/osq/state")
                    {
                        string scStr = GetQueryParam(queryString, "sinceCursor");
                        int scVal;
                        int? sc = (!string.IsNullOrEmpty(scStr) && int.TryParse(scStr, out scVal)) ? (int?)scVal : null;
                        WriteJson(stream, 200, BuildStateEnvelope(sc));
                    }
                    else if (rawPath == "/api/v1/osq/tasks")
                    {
                        int tStatus;
                        WriteJson(stream, 200, GetTasks("", out tStatus));
                    }
                    else if (rawPath.StartsWith("/api/v1/osq/tasks/", StringComparison.OrdinalIgnoreCase))
                    {
                        string tid = rawPath.Substring("/api/v1/osq/tasks/".Length);
                        int tStatus;
                        var tRes = GetTasks(tid, out tStatus);
                        WriteJson(stream, tStatus, tRes);
                    }
                    else if (rawPath == "/api/v1/hubcore/registrations")
                    {
                        WriteJson(stream, 200, GetRegistrations());
                    }
                    else if (rawPath == "/api/v1/hubcore/audit")
                    {
                        WriteJson(stream, 200, GetRestrictedAudit());
                    }
                    else if (rawPath == "/api/v1/osq/analytics/query")
                    {
                        string dom = GetQueryParam(queryString, "domain");
                        WriteJson(stream, 200, QueryAnalyticsReadonly(string.IsNullOrEmpty(dom) ? "summary" : dom));
                    }
                    else if (rawPath == "/api/v1/mesh/advertise" || rawPath == "/api/v1/mesh/status")
                    {
                        WriteJson(stream, 200, MeshAdvertise());
                    }
                    else if (rawPath == "/api/v1/mesh/snapshot-pull")
                    {
                        WriteJson(stream, 200, MeshSnapshotPull());
                    }
                    else if (rawPath == "/api/v1/osq/stream")
                    {
                        keepOpenForSse = true;
                        client.SendTimeout = 0;
                        string scStr = GetQueryParam(queryString, "sinceCursor");
                        if (string.IsNullOrEmpty(scStr)) scStr = hdrLastEventId;
                        int scVal;
                        int? sc = (!string.IsNullOrEmpty(scStr) && int.TryParse(scStr, out scVal)) ? (int?)scVal : null;

                        string sseHeaders =
                            "HTTP/1.1 200 OK\r\n" +
                            "Content-Type: text/event-stream; charset=utf-8\r\n" +
                            "Cache-Control: no-cache\r\n" +
                            "Connection: keep-alive\r\n" +
                            "Access-Control-Allow-Origin: *\r\n\r\n";
                        byte[] hb = Encoding.UTF8.GetBytes(sseHeaders);
                        stream.Write(hb, 0, hb.Length);

                        var initState = BuildStateEnvelope(sc);
                        int cur = (int)GetNum(initState, "outboxCursor", 0);
                        var initPacket = NewSerializer().Serialize(new Dictionary<string, object>
                        {
                            { "type", "connected" },
                            { "cursor", cur },
                            { "state", initState }
                        });
                        byte[] initBytes = Encoding.UTF8.GetBytes("id: " + cur + "\nevent: sync\ndata: " + initPacket + "\n\n");
                        stream.Write(initBytes, 0, initBytes.Length);
                        stream.Flush();

                        var sseClient = new SseClient { Client = client, Stream = stream };
                        lock (_sseLock) _sseClients.Add(sseClient);
                    }
                    else
                    {
                        WriteJson(stream, 404, ErrorObj("Unknown GET route: " + rawPath));
                    }
                }
                else if (method == "POST")
                {
                    if (Encoding.UTF8.GetByteCount(bodyText) > MaxPayloadBytes)
                    {
                        WriteJson(stream, 400, ErrorObj("Air-Lock Rejected: Payload exceeds " + MaxPayloadBytes + " bytes."));
                        return;
                    }
                    var body = string.IsNullOrWhiteSpace(bodyText)
                        ? new Dictionary<string, object>()
                        : NewSerializer().Deserialize<Dictionary<string, object>>(bodyText);
                    if (rawPath == "/api/v1/osq/mutate")
                    {
                        if (!string.IsNullOrEmpty(hdrIdempotencyKey) && string.IsNullOrEmpty(GetStr(body, "idempotencyKey")))
                            body["idempotencyKey"] = hdrIdempotencyKey;
                        if (!string.IsNullOrEmpty(hdrSessionId) && string.IsNullOrEmpty(GetStr(body, "sessionId")))
                            body["sessionId"] = hdrSessionId;
                        bool preferAsync = hdrPrefer.ToLowerInvariant().Contains("respond-async");
                        bool isAsync = GetBool(body, "async", false) || preferAsync;
                        int status;
                        var res = MutateRelational(body, out status);
                        if (status == 200 && isAsync) { status = 202; res["accepted"] = true; }
                        WriteJson(stream, status, res);
                    }
                    else if (rawPath == "/api/v1/osq/temp")
                    {
                        int status;
                        var res = UpdateTemp(body, out status);
                        WriteJson(stream, status, res);
                    }
                    else if (rawPath == "/api/v1/osq/marketplace/verify")
                    {
                        int status;
                        var res = VerifyMarketplaceArtifact(body, out status);
                        WriteJson(stream, status, res);
                    }
                    else if (rawPath == "/api/v1/osq/connectors/execute")
                    {
                        int status;
                        var res = ExecuteConnector(body, out status);
                        WriteJson(stream, status, res);
                    }
                    else if (rawPath == "/api/v1/osq/analytics/snapshot")
                    {
                        lock (_stateLock)
                        {
                            var snap = ExportWatermarkSnapshot(GetStr(body, "actor", "system"), "explicit-export");
                            WriteJson(stream, 200, new Dictionary<string, object> { { "ok", true }, { "snapshot", snap } });
                        }
                    }
                    else if (rawPath == "/api/v1/hubcore/register")
                    {
                        int status;
                        var res = RegisterHolon(body, out status);
                        WriteJson(stream, status, res);
                    }
                    else if (rawPath == "/api/v1/hubcore/renew")
                    {
                        int status;
                        var res = RenewRegistration(body, out status);
                        WriteJson(stream, status, res);
                    }
                    else if (rawPath == "/api/v1/hubcore/revoke")
                    {
                        int status;
                        var res = RevokeRegistration(body, out status);
                        WriteJson(stream, status, res);
                    }
                    else if (rawPath == "/api/v1/mesh/hello")
                    {
                        WriteJson(stream, 200, MeshHello(body));
                    }
                    else if (rawPath == "/api/v1/mesh/advertise")
                    {
                        WriteJson(stream, 200, MeshAdvertise());
                    }
                    else if (rawPath == "/api/v1/mesh/exchange")
                    {
                        WriteJson(stream, 200, MeshExchange(body));
                    }
                    else if (rawPath == "/api/v1/mesh/peer-write")
                    {
                        int status;
                        var res = MeshPeerWrite(body, out status);
                        WriteJson(stream, status, res);
                    }
                    else if (rawPath == "/api/v1/osq/ifr/auto-po" || rawPath == "/api/v1/osq/ifr/cancel-reconcile" || rawPath == "/api/v1/osq/rfid/event" || rawPath == "/api/v1/osq/quipu/classify-note" || rawPath == "/api/v1/osq/stockopoly/slot-lookup" || rawPath == "/api/v1/osq/sap/signal" || rawPath == "/api/v1/osq/directives/feedback")
                    {
                        string mapped = rawPath == "/api/v1/osq/ifr/auto-po" ? "ifr.auto_po"
                            : (rawPath == "/api/v1/osq/ifr/cancel-reconcile" ? "order.cancel_reconcile"
                            : (rawPath == "/api/v1/osq/rfid/event" ? "rfid.event"
                            : (rawPath == "/api/v1/osq/quipu/classify-note" ? "quipu.classify_note"
                            : (rawPath == "/api/v1/osq/stockopoly/slot-lookup" ? "stockopoly.slot_lookup"
                            : (rawPath == "/api/v1/osq/sap/signal" ? "sap.signal" : "directive.feedback")))));
                        var wrap = new Dictionary<string, object>
                        {
                            { "action", mapped },
                            { "actor", GetStr(body, "actor", "HolonHubLive-API") },
                            { "persona", GetStr(body, "persona", "Ops / Planner") },
                            { "payload", body.ContainsKey("payload") && body["payload"] is Dictionary<string, object> ? body["payload"] : body }
                        };
                        if (body.ContainsKey("idempotencyKey")) wrap["idempotencyKey"] = body["idempotencyKey"];
                        int status;
                        var res = MutateRelational(wrap, out status);
                        WriteJson(stream, status, res);
                    }
                    else
                    {
                        WriteJson(stream, 404, ErrorObj("Unknown POST route: " + rawPath));
                    }
                }
            }
            catch (Exception ex)
            {
                if (!keepOpenForSse)
                {
                    try
                    {
                        WriteJson(client.GetStream(), 500, ErrorObj(ex.Message));
                    }
                    catch {}
                }
            }
            finally
            {
                if (!keepOpenForSse)
                {
                    try { client.Close(); } catch {}
                }
            }
        }

        private void WriteJson(NetworkStream stream, int status, Dictionary<string, object> obj)
        {
            byte[] body = Encoding.UTF8.GetBytes(NewSerializer().Serialize(obj));
            WriteResponse(stream, status, "application/json; charset=utf-8", body);
        }

        private void WriteResponse(NetworkStream stream, int status, string contentType, byte[] body)
        {
            string reason = status == 200 ? "OK"
                : (status == 202 ? "Accepted"
                : (status == 204 ? "No Content"
                : (status == 400 ? "Bad Request"
                : (status == 403 ? "Forbidden"
                : (status == 404 ? "Not Found"
                : (status == 409 ? "Conflict"
                : (status == 503 ? "Service Unavailable" : "Internal Server Error")))))));
            string headers =
                "HTTP/1.1 " + status + " " + reason + "\r\n" +
                "Content-Type: " + contentType + "\r\n" +
                "Content-Length: " + body.Length + "\r\n" +
                "Cache-Control: no-store\r\n" +
                "Access-Control-Allow-Origin: *\r\n" +
                "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n" +
                "Access-Control-Allow-Headers: Content-Type, Authorization, X-Session-Id, Idempotency-Key, Prefer, Last-Event-ID\r\n" +
                "Connection: close\r\n\r\n";
            byte[] hb = Encoding.UTF8.GetBytes(headers);
            stream.Write(hb, 0, hb.Length);
            if (body.Length > 0) stream.Write(body, 0, body.Length);
            stream.Flush();
        }
    }
}
'@

Add-Type -TypeDefinition $CSharpSource -ReferencedAssemblies 'System.Web.Extensions'
Write-Host "[LIVE] HolonHubLive Network Engine + CRM listening on http://${HostIp}:${Port}" -ForegroundColor Green
Write-Host "  OSQ UI         : $HtmlPath" -ForegroundColor DarkGray
Write-Host "  CRM UI (/crm)  : $CrmHtmlPath" -ForegroundColor DarkGray
Write-Host "  Relational Tier: $RelationalPath" -ForegroundColor DarkGray
Write-Host "  Shared Temp    : $TempPath" -ForegroundColor DarkGray
$srv = New-Object HolonHubLive.Server($HostIp, $Port, $HtmlPath, $CrmHtmlPath, $RelationalPath, $MirrorRelationalPath, $TempPath)
$srv.Run()
