# Build the Windows installer for a release, on Windows:
#
#     pwsh -File scripts/build-windows-installer.ps1 0.2.1   (PowerShell 7)
#
# The installer is a launcher: a copy of uv plus Start menu and desktop
# shortcuts that run that exact PyPI release (see packaging/windows/installer.iss).
# Output: dist/windows/Signal-Chain-Generator-VERSION-Setup.exe

param([Parameter(Mandatory = $true)][string]$Version)

$ErrorActionPreference = "Stop"
$UvVersion = if ($env:UV_VERSION) { $env:UV_VERSION } else { "0.11.32" }
$Root = Split-Path -Parent $PSScriptRoot
$Stage = Join-Path $Root "dist\windows"

if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

# uv for 64-bit Windows, checked against its published checksum.
$base = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
$zip = Join-Path $env:TEMP "uv-$UvVersion.zip"
Invoke-WebRequest -Uri $base -OutFile $zip -MaximumRetryCount 3 -RetryIntervalSec 5
$sumFile = "$zip.sha256"
Invoke-WebRequest -Uri "$base.sha256" -OutFile $sumFile -MaximumRetryCount 3 -RetryIntervalSec 5
$expected = ((Get-Content -Raw $sumFile).Trim() -split '\s+')[0]
$actual = (Get-FileHash -Algorithm SHA256 $zip).Hash
if ($expected.ToLower() -ne $actual.ToLower()) { throw "uv download checksum mismatch" }
$unzipped = Join-Path $env:TEMP "uv-$UvVersion"
Expand-Archive -Force -Path $zip -DestinationPath $unzipped
foreach ($exe in "uv.exe", "uvw.exe") {
    $found = Get-ChildItem -Recurse -Path $unzipped -Filter $exe | Select-Object -First 1
    if (-not $found) { throw "$exe is not in the uv download" }
    Copy-Item $found.FullName (Join-Path $Stage $exe)
}

Copy-Item (Join-Path $Root "LICENSE") (Join-Path $Stage "LICENSE.txt")
Copy-Item (Join-Path $Root "NOTICE") (Join-Path $Stage "NOTICE.txt")

# The app's icon as a Windows .ico, from the web app's 512 px PNG.
$png = Join-Path $Root "stompxl\static\icon-512.png"
$ico = Join-Path $Stage "app.ico"
& "$Stage\uv.exe" run --no-project --python 3.13 --with pillow python -c `
    "import sys; from PIL import Image; Image.open(sys.argv[1]).save(sys.argv[2], sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])" `
    $png $ico
if ($LASTEXITCODE -ne 0) { throw "icon conversion failed" }

# Inno Setup's compiler: preinstalled on GitHub's Windows images, else Chocolatey.
$iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    foreach ($dir in "${env:ProgramFiles(x86)}\Inno Setup 6", "$env:ProgramFiles\Inno Setup 6", "$env:LOCALAPPDATA\Programs\Inno Setup 6") {
        if (Test-Path "$dir\ISCC.exe") { $iscc = "$dir\ISCC.exe"; break }
    }
}
if (-not $iscc) {
    choco install innosetup -y --no-progress | Out-Null
    $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
}
& $iscc /Q "/DVersion=$Version" "/DStage=$Stage" (Join-Path $Root "packaging\windows\installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$out = Join-Path $Stage "out\Signal-Chain-Generator-$Version-Setup.exe"
if (-not (Test-Path $out)) { throw "installer was not built" }
Move-Item $out (Join-Path $Stage "Signal-Chain-Generator-$Version-Setup.exe") -Force
Write-Output "Built dist\windows\Signal-Chain-Generator-$Version-Setup.exe"
