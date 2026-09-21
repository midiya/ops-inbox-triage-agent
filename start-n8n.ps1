<#
.SYNOPSIS
    Starts a Cloudflare tunnel to local n8n, then starts n8n wired to the public URL.

.DESCRIPTION
    n8n's built-in --tunnel relies on hooks.n8n.cloud, which is not reachable from
    this network (TCP:443 refused). This script substitutes a Cloudflare quick
    tunnel, which is reachable, and exports WEBHOOK_URL so the Webhook node shows
    the public URL instead of localhost.

    Runs cloudflared via Docker so nothing extra needs installing.
    Ctrl+C stops n8n and tears the tunnel down.

.PARAMETER Port
    Local port n8n listens on. Default 5678.

.PARAMETER TimeoutSeconds
    How long to wait for cloudflared to report a public URL. Default 60.

.PARAMETER KeepTelemetry
    Leave n8n's telemetry/community-node fetches enabled. They fail noisily on
    this network (SNI filtering kills the TLS handshake), so they are off by default.

.EXAMPLE
    .\start-n8n.ps1

.EXAMPLE
    .\start-n8n.ps1 -Port 5678 -TimeoutSeconds 90
#>
[CmdletBinding()]
param(
    [int]$Port = 5678,
    [int]$TimeoutSeconds = 60,
    [switch]$KeepTelemetry
)

$ErrorActionPreference = 'Stop'

$ContainerName = 'n8n-quick-tunnel'
$UrlPattern    = 'https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com'

$logDir = Join-Path $env:TEMP 'n8n-tunnel'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$outLog = Join-Path $logDir 'cloudflared.out.log'
$errLog = Join-Path $logDir 'cloudflared.err.log'

# Under $ErrorActionPreference='Stop', ANY stderr output from a native exe is
# promoted to a terminating error - even on exit code 0, and even with 2>$null.
# Docker writes to stderr routinely (daemon down, no such container), so every
# docker call goes through here: stderr is swallowed and we judge by exit code.
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Arguments = @()
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | Out-Null
        return $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $prev }
}

function Stop-Tunnel {
    Write-Host "`nTearing down tunnel..." -ForegroundColor DarkGray
    Invoke-Native docker @('rm', '-f', $ContainerName) | Out-Null
}

# --- preflight ---------------------------------------------------------------

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker not found on PATH. Install Docker Desktop, or install cloudflared directly and adapt this script."
}

# Use `docker version`, not `docker info`: with the daemon down, this CLI build
# panics inside its output template and dumps a Go stack trace instead of an error.
if ((Invoke-Native docker @('version', '--format', '{{.Server.Version}}')) -ne 0) {
    throw "Docker daemon is not running. Start Docker Desktop, wait for it to report Running, then re-run."
}

if (-not (Get-Command npx -ErrorAction SilentlyContinue)) {
    throw "npx not found on PATH. Install Node.js."
}

# A stale container from a previous run would hold the name.
Invoke-Native docker @('rm', '-f', $ContainerName) | Out-Null
Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue

# --- start the tunnel --------------------------------------------------------

Write-Host "Starting Cloudflare tunnel to host port $Port ..." -ForegroundColor Cyan

# host.docker.internal resolves to the Windows host from inside the container.
# The tunnel comes up before n8n does; it will 502 until n8n is listening, which
# is harmless - the public hostname is already assigned by then.
$dockerArgs = @(
    'run', '--rm', '--name', $ContainerName,
    '--add-host', 'host.docker.internal:host-gateway',
    'cloudflare/cloudflared:latest',
    'tunnel', '--no-autoupdate', '--url', "http://host.docker.internal:$Port"
)

$proc = Start-Process -FilePath 'docker' -ArgumentList $dockerArgs `
    -NoNewWindow -PassThru `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog

try {
    # cloudflared prints the assigned hostname to stderr, inside a banner box.
    $publicUrl = $null
    $deadline  = (Get-Date).AddSeconds($TimeoutSeconds)

    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) {
            Write-Host (Get-Content $errLog -Raw -ErrorAction SilentlyContinue)
            throw "cloudflared exited early (code $($proc.ExitCode)). See $errLog"
        }

        # Read shared so we do not fight cloudflared for the file handle.
        $text = ''
        foreach ($f in @($errLog, $outLog)) {
            if (Test-Path $f) {
                try {
                    $stream = [System.IO.File]::Open($f, 'Open', 'Read', 'ReadWrite')
                    $reader = New-Object System.IO.StreamReader($stream)
                    $text += $reader.ReadToEnd()
                    $reader.Close(); $stream.Close()
                } catch { }
            }
        }

        $m = [regex]::Match($text, $UrlPattern)
        if ($m.Success) { $publicUrl = $m.Value; break }

        Start-Sleep -Milliseconds 500
    }

    if (-not $publicUrl) {
        throw "No trycloudflare URL after $TimeoutSeconds s. Check $errLog - if the TLS handshake is being reset, Cloudflare is blocked too and you need a different relay."
    }

    Write-Host "Tunnel up: $publicUrl" -ForegroundColor Green

    # --- start n8n -----------------------------------------------------------

    $env:WEBHOOK_URL = $publicUrl

    if (-not $KeepTelemetry) {
        # Every one of these calls an n8n-owned endpoint that this network drops
        # mid-TLS. Failures are cosmetic but they bury real log output, and the
        # telemetry proxy has a bug that throws "Cannot remove headers after they
        # are sent" whenever its own request fails.
        $env:N8N_DIAGNOSTICS_ENABLED           = 'false'
        $env:N8N_VERSION_NOTIFICATIONS_ENABLED = 'false'
        $env:N8N_TEMPLATES_ENABLED             = 'false'
        $env:N8N_COMMUNITY_PACKAGES_ENABLED    = 'false'
        $env:N8N_DIAGNOSTICS_CONFIG_FRONTEND   = ''
        $env:N8N_DIAGNOSTICS_CONFIG_BACKEND    = ''
        $env:EXTERNAL_FRONTEND_HOOKS_URLS      = ''
    }

    # The Python task runner cannot start under npx on Windows: the npm package
    # omits @n8n/task-runner-python entirely, so the venv it hardcodes is absent.
    # Silence the startup warning rather than leave a red herring in the log.
    $env:N8N_PYTHON_ENABLED = 'false'

    Write-Host ""
    Write-Host "  Editor   http://localhost:$Port"      -ForegroundColor Gray
    Write-Host "  Webhooks $publicUrl"                  -ForegroundColor Gray
    Write-Host "  Ctrl+C to stop both."                 -ForegroundColor DarkGray
    Write-Host ""

    npx n8n start
}
finally {
    Stop-Tunnel
}
