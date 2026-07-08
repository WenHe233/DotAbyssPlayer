<#
  build.ps1 — one-shot packaging for the DotAbyss desktop client (Windows x64).

  Chains the three toolchains into a single NSIS installer + a portable zip:
    1. dotnet publish  -> self-contained DotAbyssClient.exe (the catalog/downloader)
    2. PyInstaller     -> DotAbyssBackend/ (frozen server + extractor + UnityPy + vgmstream deps)
    3. cargo tauri build -> WebView2 shell + NSIS installer, bundling (1)(2)+vgmstream as resources

  Prerequrisites (installed automatically if missing): .NET 8 SDK, Rust, Node, Python venv.
  Run from the repo root:  pwsh -File build/build.ps1
#>
[CmdletBinding()]
param(
  [switch]$SkipDownloader,
  [switch]$SkipBackend,
  [switch]$SkipShell,
  [switch]$Portable
)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $Repo
# Prefer the repo venv (dev); fall back to PATH python (CI).
$VenvPy = Join-Path $Repo ".venv/Scripts/python.exe"
$Py = if (Test-Path $VenvPy) { $VenvPy } else { (Get-Command python).Source }
$ResBin = Join-Path $Repo "desktop/src-tauri/resources/bin"
$ResBackend = Join-Path $Repo "desktop/src-tauri/resources/backend"
New-Item -ItemType Directory -Force -Path $ResBin, $ResBackend | Out-Null

Write-Host "== [1/4] .NET downloader ==" -ForegroundColor Cyan
if (-not $SkipDownloader) {
  dotnet publish src/DotAbyssClient -c Release -r win-x64 --self-contained `
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true `
    -o build/_downloader
  Copy-Item build/_downloader/DotAbyssClient.exe (Join-Path $ResBin "DotAbyssClient.exe") -Force
}

Write-Host "== [2/4] vgmstream ==" -ForegroundColor Cyan
$vg = Join-Path $Repo "tools/bin/vgmstream"
if (Test-Path $vg) {
  Copy-Item $vg (Join-Path $ResBin "vgmstream") -Recurse -Force
} else {
  Write-Warning "tools/bin/vgmstream not found — download vgmstream-cli win64 into it before shipping."
}

Write-Host "== [3/4] PyInstaller backend ==" -ForegroundColor Cyan
if (-not $SkipBackend) {
  & $Py -m pip install --quiet --upgrade pyinstaller
  & $Py -m PyInstaller --noconfirm --clean build/backend.spec
  if (Test-Path $ResBackend) { Remove-Item $ResBackend -Recurse -Force }
  Copy-Item dist/DotAbyssBackend (Join-Path $Repo "desktop/src-tauri/resources/backend") -Recurse -Force
  # Flatten: resources/backend/DotAbyssBackend/* -> resources/backend/* so the shell finds the exe.
  $inner = Join-Path $ResBackend "DotAbyssBackend"
  if (Test-Path $inner) {
    Get-ChildItem $inner | Move-Item -Destination $ResBackend -Force
    Remove-Item $inner -Recurse -Force
  }
}

Write-Host "== [4/4] Tauri shell + installer ==" -ForegroundColor Cyan
if (-not $SkipShell) {
  if (-not (Get-Command cargo-tauri -ErrorAction SilentlyContinue)) {
    cargo install tauri-cli --version "^2" --locked
  }
  Push-Location desktop/src-tauri
  cargo tauri build
  Pop-Location
  Write-Host "Installer -> desktop/src-tauri/target/release/bundle/nsis/" -ForegroundColor Green
}

if ($Portable) {
  Write-Host "== portable zip ==" -ForegroundColor Cyan
  $rel = Join-Path $Repo "desktop/src-tauri/target/release"
  $stage = Join-Path $Repo "build/_portable/DotAbyssPlayer"
  if (Test-Path (Join-Path $Repo "build/_portable")) { Remove-Item (Join-Path $Repo "build/_portable") -Recurse -Force }
  New-Item -ItemType Directory -Force -Path $stage | Out-Null
  Copy-Item (Join-Path $rel "DotAbyssPlayer.exe") $stage -Force
  Copy-Item (Join-Path $Repo "desktop/src-tauri/resources") (Join-Path $stage "resources") -Recurse -Force
  New-Item -ItemType File -Force -Path (Join-Path $stage "portable.txt") | Out-Null
  Compress-Archive -Path $stage -DestinationPath (Join-Path $Repo "build/DotAbyssPlayer-portable.zip") -Force
  Write-Host "Portable -> build/DotAbyssPlayer-portable.zip" -ForegroundColor Green
}

Write-Host "Done." -ForegroundColor Green
