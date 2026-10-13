; Nano for Windows — Inno Setup installer script (nanowin flavor).
;
; Built by .github/workflows/build-windows.yml AFTER `flet build windows`.
; Version, exe name and source dir are injected per build via /D defines so
; this file never hardcodes release-specific values:
;   ISCC.exe /DAppVersion="0.17.3" /DExeName="nano.exe" /DSourceDir="C:\...\Release" windows\installer.iss
;
; AppId is stable across releases (generated once): upgrades replace the
; install in place instead of registering a second "Nano" entry.

#define AppName "Nano | نانو"
#define AppVersion "0.0.0"
#define ExeName "nano.exe"
#define SourceDir "."

[Setup]
AppId={{149FA2FB-D177-479B-B010-FD608A765378}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={autopf}\Nano
OutputDir=build\windows\installer
OutputBaseFilename=Nano-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
WizardStyle=modern
UninstallDisplayName={#AppName}

[Languages]
Name: "arabic"; MessagesFile: "compiler:Languages\Arabic.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; Flags: unchecked

[Run]
Filename: "{app}\{#ExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
