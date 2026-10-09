<#
MR Pilot - instal & kelola dengan Docker Desktop (Windows).

  setup.bat                  instal: Docker Desktop (jika belum ada) -> build -> wizard -> jalan
  setup.bat -Server          dashboard bisa dibuka dari jaringan (bind 0.0.0.0)
  setup.bat ci               pasang CI/CD (GitHub/GitLab) + deploy otomatis ke server
  setup.bat update | start | stop | restart | status | logs | doctor | config | shell | demo
  setup.bat password [-Reset]   lihat / buat ulang password dashboard
  setup.bat reset [-All] [-Yes] hapus riwayat MR, aktivitas & log (token/.env, config, standar tetap)

Opsi: -Server  -Port 8787  -WithClaudeCode  -WithOllama  -OllamaModel qwen2.5-coder:14b  -Yes
#>
param(
  [Parameter(Position = 0)]
  [ValidateSet("install", "update", "ci", "start", "stop", "restart", "status", "logs", "doctor", "config", "shell", "demo", "password", "reset")]
  [string]$Command = "install",
  [switch]$Server,
  [switch]$Local,
  [int]$Port = 0,
  [switch]$WithClaudeCode,
  [switch]$WithOllama,
  [string]$OllamaModel = "",
  [switch]$Yes,
  [switch]$Reset,
  [switch]$All
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function AskYN($q, $def) {
  if ($Yes) { return $def }
  $d = if ($def) { "Y/n" } else { "y/N" }
  $a = Read-Host "    $q [$d]"
  if ([string]::IsNullOrWhiteSpace($a)) { return $def }
  return $a -match '^[YyJj]'
}
function Get-Kv($key, $file = ".env") {
  if (-not (Test-Path $file)) { return "" }
  $line = Get-Content $file | Where-Object { $_ -match "^$key=" } | Select-Object -Last 1
  if ($line) { return ($line -split "=", 2)[1].Trim('"') } else { return "" }
}
function Set-Kv($key, $value) {
  # Selalu pakai List: pipeline PowerShell mengubah array 1 item jadi string, dan += lalu menyambung teks.
  $lines = [System.Collections.Generic.List[string]]::new()
  if (Test-Path .env) { foreach ($l in [IO.File]::ReadAllLines((Join-Path $PSScriptRoot ".env"))) { $lines.Add($l) } }
  $found = $false
  for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match "^$key=") { $lines[$i] = "$key=$value"; $found = $true }
  }
  if (-not $found) { $lines.Add("$key=$value") }
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [IO.File]::WriteAllLines((Join-Path $PSScriptRoot ".env"), $lines.ToArray(), $utf8NoBom)
}
function Repair-EnvFile {
  # .env dari versi lama setup.ps1 bisa berisi semua nilai dalam satu baris (MRP_BIND=127.0.0.1MRP_PORT=...).
  if ((Test-Path .env) -and ((Get-Content .env -Raw) -match 'MRP_BIND=[^\r\n]*MRP_PORT=')) {
    Say "File .env rusak (dari setup versi lama), dibuat ulang."
    Move-Item .env ".env.rusak.bak" -Force
  }
}

# Docker writes progress ("Container x Stopping") to stderr. In Windows PowerShell 5.1 with
# $ErrorActionPreference = "Stop", redirecting a native command's stderr turns that text into a
# terminating error. Run quiet docker calls with Continue and judge success by the exit code only.
function Invoke-DockerQuiet {
  $old = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try { & docker @args *> $null } finally { $ErrorActionPreference = $old }
  return $LASTEXITCODE
}
function Ensure-Docker {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Say "Docker Desktop belum terpasang."
    if (-not (AskYN "Instal Docker Desktop sekarang lewat winget?" $true)) { exit 1 }
    winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
    Write-Host ""
    Write-Host "    Docker Desktop terpasang. Restart Windows bila diminta, buka Docker Desktop sekali" -ForegroundColor Yellow
    Write-Host "    (setujui syarat & tunggu status 'running'), lalu jalankan setup.bat lagi." -ForegroundColor Yellow
    exit 0
  }
  if ((Invoke-DockerQuiet info) -ne 0) {
    $exe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $exe) {
      Say "Menyalakan Docker Desktop…"
      Start-Process $exe
      for ($i = 0; $i -lt 60; $i++) { Start-Sleep 3; if ((Invoke-DockerQuiet info) -eq 0) { break } }
    }
    if ((Invoke-DockerQuiet info) -ne 0) { Write-Host "Docker belum berjalan. Buka Docker Desktop lalu ulangi." -ForegroundColor Red; exit 1 }
  }
  if ((Invoke-DockerQuiet compose version) -ne 0) { Write-Host "'docker compose' tidak tersedia. Update Docker Desktop." -ForegroundColor Red; exit 1 }
}
function DC { & docker compose @args; if ($LASTEXITCODE -ne 0) { throw "docker compose $($args -join ' ') gagal ($LASTEXITCODE)" } }

function Wait-Healthy {
  $p = Get-Kv "MRP_PORT"; if (-not $p) { $p = 8787 }
  Write-Host -NoNewline "    menunggu MR Pilot siap "
  for ($i = 0; $i -lt 40; $i++) {
    try { Invoke-WebRequest "http://127.0.0.1:$p/healthz" -UseBasicParsing -TimeoutSec 2 | Out-Null; Write-Host "OK"; return } catch { }
    Write-Host -NoNewline "."; Start-Sleep 2
  }
  Write-Host ""; Say "Belum merespons. Cek log: setup.bat logs"
}
function Summary {
  $p = Get-Kv "MRP_PORT"; if (-not $p) { $p = 8787 }
  $hostName = "127.0.0.1"
  if ((Get-Kv "MRP_BIND") -eq "0.0.0.0") { $hostName = $env:COMPUTERNAME }
  $pw = Get-Kv "DASHBOARD_PASSWORD" "data\.env"
  Write-Host ""
  Say "MR Pilot berjalan"
  Write-Host "    Dashboard : http://${hostName}:$p"
  Write-Host "    Password  : $pw"
  Write-Host "    Perintah  : setup.bat logs | status | doctor | update | stop | ci"
  if ((Get-Kv "MRP_BIND") -eq "0.0.0.0") {
    Write-Host "    Dari HP/laptop lain: izinkan port $p di Windows Firewall bila belum bisa dibuka." -ForegroundColor Yellow
  }
}

switch ($Command) {
  "install" {
    Ensure-Docker
    Repair-EnvFile
    Say "Konfigurasi Docker"
    $flags = $Server -or $Local -or $Port -or $WithClaudeCode -or $WithOllama
    if (-not (Test-Path .env) -or $flags -or -not (Get-Kv "MRP_PORT")) {
      $bind = if ($Server) { "0.0.0.0" } elseif ($Local) { "127.0.0.1" } elseif (AskYN "Dashboard bisa dibuka dari komputer lain di jaringan (mode server)?" $false) { "0.0.0.0" } else { "127.0.0.1" }
      $claude = if ($WithClaudeCode) { $true } else { AskYN "Pasang CLI Claude Code di container (pakai langganan Claude)?" $false }
      $ollama = if ($WithOllama) { $true } else { AskYN "Jalankan AI lokal Ollama di Docker juga?" $false }
      Set-Kv "MRP_BIND" $bind
      $pp = if ($Port) { $Port } elseif (Get-Kv "MRP_PORT") { Get-Kv "MRP_PORT" } else { 8787 }
      Set-Kv "MRP_PORT" $pp
      Set-Kv "INSTALL_CLAUDE_CODE" ($(if ($claude) { "true" } else { "false" }))
      Set-Kv "MRP_UID" "1000"; Set-Kv "MRP_GID" "1000"
      Set-Kv "COMPOSE_PROFILES" ($(if ($ollama) { "local-ai" } else { "" }))
      Set-Kv "MRP_OLLAMA_BUNDLED" ($(if ($ollama) { "1" } else { "0" }))
      if (-not (Get-Kv "TZ")) { Set-Kv "TZ" "Asia/Jakarta" }
    }
    New-Item -ItemType Directory -Force -Path "data\home" | Out-Null
    Say "Build image (beberapa menit pertama kali)"
    DC build
    Say "Wizard konfigurasi"
    DC run --rm mr-pilot setup
    Say "Menjalankan"
    DC up -d
    if ((Get-Kv "COMPOSE_PROFILES") -eq "local-ai") {
      $m = if ($OllamaModel) { $OllamaModel } else { Get-Kv "LOCAL_AI_MODEL" "data\.env" }
      if (-not $m) { $m = "qwen2.5-coder:14b" }
      Say "Mengunduh model Ollama $m (sekali saja, bisa lama)"
      docker compose exec -T ollama ollama pull $m
    }
    Wait-Healthy; Summary
  }
  "update" {
    Ensure-Docker
    if (Test-Path .git) { Say "git pull"; git pull --ff-only }
    Say "Build & restart"
    DC build; DC up -d --remove-orphans; Wait-Healthy; Summary
  }
  "ci" {
    Ensure-Docker
    if (-not (Test-Path "data\config.yaml")) { Write-Host "Jalankan setup.bat dulu (config belum ada)." -ForegroundColor Red; exit 1 }
    DC build | Out-Null
    DC run --rm -v "${PSScriptRoot}:/src" -e MRP_SRC=/src -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e "GIT_CONFIG_VALUE_0=*" mr-pilot setup-ci
  }
  "start" { Ensure-Docker; DC up -d; Wait-Healthy; Summary }
  "stop" { Ensure-Docker; DC down }
  "restart" { Ensure-Docker; DC restart mr-pilot; Wait-Healthy }
  "status" { Ensure-Docker; DC ps }
  "reset" {
    Ensure-Docker
    Invoke-DockerQuiet compose stop mr-pilot | Out-Null   # database tidak boleh sedang dipakai
    $rargs = @("reset"); if ($Yes) { $rargs += "--yes" }; if ($All) { $rargs += "--all" }
    docker compose run --rm mr-pilot @rargs
    if ($LASTEXITCODE -eq 0) {
      DC up -d --force-recreate mr-pilot   # container baru = riwayat log Docker juga bersih
      Wait-Healthy
    } else {
      Invoke-DockerQuiet compose start mr-pilot | Out-Null
    }
  }
  "logs" { Ensure-Docker; docker compose logs -f --tail 100 mr-pilot }
  "doctor" { Ensure-Docker; docker compose run --rm -T mr-pilot doctor }
  "password" {
    Ensure-Docker
    if ($Reset) { DC run --rm -T mr-pilot password --reset; DC restart mr-pilot; Wait-Healthy }
    else { docker compose run --rm -T mr-pilot password }
  }
  "config" {
    Ensure-Docker
    # hentikan bot dulu: wizard memakai bot Telegram yang sama (409) dan menulis ulang .env
    Invoke-DockerQuiet compose stop mr-pilot | Out-Null
    DC run --rm mr-pilot setup
    DC up -d mr-pilot
    Wait-Healthy
  }
  "shell" { Ensure-Docker; docker compose exec mr-pilot bash }
  "demo" {
    Ensure-Docker; DC build | Out-Null
    $dp = if ($Port) { $Port } else { 8788 }
    Write-Host "Dashboard demo: http://127.0.0.1:$dp  (password: demo, Ctrl+C untuk berhenti)"
    docker compose run --rm -p "127.0.0.1:${dp}:8787" mr-pilot demo
  }
}
