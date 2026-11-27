; Inno Setup script for the Windows installer. Built by
; scripts/build-windows-installer.ps1, which passes /DVersion=X.Y.Z and puts
; uv.exe, uvw.exe and the icon in the staging folder (/DStage=...).
;
; Like the Mac app, the install is a launcher: a copy of uv plus a shortcut that
; runs that exact PyPI release. uvw.exe is uv without a console window, so a
; click shows nothing but the browser.

#ifndef Version
  #error Pass /DVersion=X.Y.Z
#endif
#ifndef Stage
  #error Pass /DStage=folder
#endif

#define AppName "Signal Chain Generator"
#define RunArgs "tool run --python 3.13 --from signal-chain-generator==" + Version + " signal-chain-generator"

[Setup]
AppId={{6C1F7A52-3B8E-4F0B-9E43-5C2D1A7E9B10}
AppName={#AppName}
AppVersion={#Version}
AppVerName={#AppName} {#Version}
AppPublisher=Signal Chain Generator contributors
AppPublisherURL=https://github.com/kippjohnsonai-a11y/signal-chain-generator
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user install: no administrator prompt.
PrivilegesRequired=lowest
OutputDir={#Stage}\out
OutputBaseFilename=Signal-Chain-Generator-{#Version}-Setup
SetupIconFile={#Stage}\app.ico
UninstallDisplayIcon={app}\app.ico
LicenseFile={#Stage}\LICENSE.txt
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "{#Stage}\uv.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\uvw.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\app.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Stage}\NOTICE.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\uvw.exe"; Parameters: "{#RunArgs} --launch"; IconFilename: "{app}\app.ico"; Comment: "Open {#AppName} in your browser"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\uvw.exe"; Parameters: "{#RunArgs} --launch"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Run]
; Download Python and the app now, so the first click opens quickly.
Filename: "{app}\uv.exe"; Parameters: "{#RunArgs} --help"; Flags: runhidden waituntilterminated; StatusMsg: "Downloading Python and the app (a minute or two the first time)..."
Filename: "{app}\uvw.exe"; Parameters: "{#RunArgs} --launch"; Description: "Open {#AppName} now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; Stop a running copy so its files can be removed. Your library stays in
; %APPDATA%\{#AppName}.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -Command ""$f = Join-Path $env:APPDATA '{#AppName}\server.pid'; if (Test-Path $f) {{ Stop-Process -Id (Get-Content $f) -Force -ErrorAction SilentlyContinue }"""; Flags: runhidden; RunOnceId: "StopApp"
