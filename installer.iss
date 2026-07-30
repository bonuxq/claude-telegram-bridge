; Installer for the Claude Code <-> Telegram bridge.
;
; Per-user on purpose: it needs no administrator, and the hooks it writes live
; in the user's own ~/.claude/settings.json anyway. Asking for elevation would
; only make the two halves disagree about whose machine this is.

#define AppName "Claude Telegram Bridge"
#define AppShort "ClaudeTelegram"
; build.py passes the real one from claudetg/version.py, so the installer and
; the updater can never disagree about which release this is.
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#define AppPublisher "Bonux"
#define AppURL "https://github.com/bonuxq/claude-telegram-bridge"

[Setup]
AppId={{6E2B7C41-3F2A-4C77-9E2D-4B7C1A9F5D30}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppSupportURL={#AppURL}
DefaultDirName={localappdata}\{#AppShort}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename={#AppShort}-Setup-{#AppVersion}
SetupIconFile=widget.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppShort}.exe
; Offer to close a running copy instead of failing on a locked file.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "uk"; MessagesFile: "compiler:Languages\Ukrainian.isl"
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "startup"; Description: "{cm:AutoStartProgram,{#AppName}}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole one-folder build: four executables plus the shared runtime.
Source: "dist\ClaudeTelegram\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
Source: "FEATURES.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppShort}.exe"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppShort}.exe"; Tasks: desktopicon
; Both halves at login: the daemon serves the hooks, the widget is the face.
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppShort}.exe"; Tasks: startup
Name: "{userstartup}\{#AppName} daemon"; Filename: "{app}\claudetg-daemon.exe"; Tasks: startup

[Run]
; Wire the Claude Code hooks. Without them the bridge sees nothing at all,
; so this is not optional and not a checkbox.
Filename: "{app}\claudetg-daemon.exe"; Parameters: "--install-hooks"; Flags: runhidden waituntilterminated; StatusMsg: "Wiring Claude Code hooks..."
Filename: "{app}\claudetg-daemon.exe"; Description: "Start the bridge"; Flags: nowait postinstall runhidden
Filename: "{app}\{#AppShort}.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Take the hooks back out before the executable that knows how to do it goes.
Filename: "{app}\claudetg-daemon.exe"; Parameters: "--uninstall-hooks"; Flags: runhidden waituntilterminated; RunOnceId: "unhook"

[UninstallDelete]
; Runtime files the app writes next to itself; the config is left behind on
; purpose so a reinstall does not lose the token and the project list.
Type: files; Name: "{app}\state.json"
Type: files; Name: "{app}\usage.json"
Type: files; Name: "{app}\widget.json"
Type: files; Name: "{app}\widget.ico"
Type: files; Name: "{app}\widget-tray.ico"
Type: files; Name: "{app}\daemon.log"
Type: files; Name: "{app}\widget.log"

[Code]
procedure CurUninstallStepChanged(CurStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  { The daemon holds its own DLLs open; stop both halves before deleting. }
  if CurStep = usUninstall then
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im claudetg-daemon.exe',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im ClaudeTelegram.exe',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
