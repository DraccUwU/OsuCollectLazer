; Inno Setup script for the Windows installer.
;
;   iscc /DMyAppVersion=1.1.0 /DSourceDir=..\dist-portable\OsuCollectLazer packaging\installer.iss
;
; CI compiles exactly this (see .github/workflows/release.yml). The installer lays down the
; folder build (which starts instantly, unlike the single-file exe), adds Start-menu and
; optional desktop shortcuts, and registers an uninstaller. It installs per user by default
; so there is no UAC prompt; the wizard's first page lets anyone choose all users instead.

#ifndef MyAppVersion
  #define MyAppVersion "1.1.0"
#endif
#ifndef MyAppPublisher
  #define MyAppPublisher "DraccUwU"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist-portable\OsuCollectLazer"
#endif

#define MyAppName "OsuCollectLazer"
#define MyAppExeName "OsuCollectLazer.exe"
#define MyAppURL "https://github.com/DraccUwU/OsuCollectLazer"

[Setup]
; stable id: a new GUID would make Windows treat every release as a different app
AppId={{7B0E4E2C-3F5C-4C58-9C57-2E1E1B7A6E10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
LicenseFile=..\LICENSE
OutputDir=..\dist-installer
OutputBaseFilename=OsuCollectLazer-Setup
SetupIconFile=..\app\web\favicon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; per-user by default (no admin prompt); the first page offers "all users"
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; the app is a tray-less window; closing the installer's running copy is fine
CloseApplications=yes
RestartApplications=no
MinVersion=10.0.17763
VersionInfoVersion={#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; the frozen app: exe + _internal + the web UI it serves
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "osu!collector collections into osu!lazer"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; Comment: "osu!collector collections into osu!lazer"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
