; Inno Setup script for ClipMonster-Setup.exe. build.py runs it after PyInstaller:
;   iscc /DAppVersion=2.8.0 /DNumericVersion=2.8.0.0 /DSourceDir=..\dist\ClipMonster /DOutputDir=..\dist ClipMonster.iss
;
; Installs for the current user only (no admin prompt) into %LOCALAPPDATA%\Programs\ClipMonster: out of
; OneDrive, and somewhere the in-app updater can replace the app's files later. Settings and history
; live in %APPDATA%\ClipMonster, and downloads, models, updates and temporary work in
; %LOCALAPPDATA%\ClipMonster; installing never touches them, and uninstalling asks whether to delete
; them. Your clips are never touched.
;
; Running it over an installed copy updates it: automation is stopped first (and started again after),
; and an open ClipMonster window is closed, so it saves. On a first install, automation running from a
; copy unzipped by hand is stopped the same way, and starts again from the installed copy.
;
; A copy unzipped by hand installs itself with (updater.run_setup):
;   ClipMonster-Setup.exe /SILENT /SUPPRESSMSGBOXES /NORESTART /LAUNCH=1 /STOPFROM=<that copy's folder>
; /LAUNCH=1 opens the installed ClipMonster afterwards, which a silent install otherwise doesn't.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef NumericVersion
  #define NumericVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\ClipMonster"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
; Never change AppId: Windows uses it to recognise the installed app for updates and uninstalling,
; and the app finds its own uninstall entry by it (updater.UNINSTALL_KEY).
AppId={{92B949DB-584A-40C4-892B-F7016573CE28}
AppName=ClipMonster
AppVersion={#AppVersion}
AppVerName=ClipMonster {#AppVersion}
AppPublisher=iSlipperyX
AppPublisherURL=https://github.com/iSlipperyX/ClipMostr
AppSupportURL=https://github.com/iSlipperyX/ClipMostr/issues
AppUpdatesURL=https://github.com/iSlipperyX/ClipMostr/releases
VersionInfoVersion={#NumericVersion}
VersionInfoProductVersion={#NumericVersion}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoDescription=ClipMonster Setup
DefaultDirName={localappdata}\Programs\ClipMonster
DefaultGroupName=ClipMonster
DisableProgramGroupPage=yes
; Asked on the first install only; an update goes where ClipMonster already is.
DisableDirPage=auto
UsePreviousAppDir=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=ClipMonster-Setup
SetupIconFile=clipmonster.ico
UninstallDisplayIcon={app}\ClipMonster.exe
UninstallDisplayName=ClipMonster
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Close an open ClipMonster window (it saves on exit) before replacing its files.
CloseApplications=yes
RestartApplications=no
; While this exists, ClipMonster doesn't start an update of its own (updater.setup_running).
SetupMutex=ClipMonsterSetup,Global\ClipMonsterSetup

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; A clean copy each time, so files a newer version dropped don't linger.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\whisper-cpp"

[Icons]
Name: "{autoprograms}\ClipMonster"; Filename: "{app}\ClipMonster.exe"
Name: "{autodesktop}\ClipMonster"; Filename: "{app}\ClipMonster.exe"; Tasks: desktopicon

[Run]
; Automation that was running before carries on, in the new version.
Filename: "{app}\ClipMonster.exe"; Parameters: "--automation"; Flags: nowait runasoriginaluser; Check: AutomationWasRunning
Filename: "{app}\ClipMonster.exe"; Description: "Start ClipMonster"; Flags: nowait postinstall skipifsilent runasoriginaluser
Filename: "{app}\ClipMonster.exe"; Flags: nowait runasoriginaluser; Check: LaunchAfterSilentInstall

[UninstallDelete]
; Left behind by the in-app updater when something held a file open.
Type: filesandordirs; Name: "{app}\.cm-old*"

[Code]
var
  WasRunning: Boolean;

function AutomationWasRunning: Boolean;
begin
  Result := WasRunning;
end;

function LaunchAfterSilentInstall: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:LAUNCH|0}') = '1');
end;

{ The ClipMonster whose ytclip.exe can stop automation: the installed one, else the copy that started
  this install (/STOPFROM=), else the one Start with Windows runs ("<folder>\ClipMonster.exe" --automation). }
function AutomationFolder: String;
var
  Cmd: String;
  P: Integer;
begin
  Result := ExpandConstant('{app}');
  if FileExists(Result + '\ytclip.exe') then
    Exit;
  Result := ExpandConstant('{param:STOPFROM|}');
  if (Result <> '') and FileExists(Result + '\ytclip.exe') then
    Exit;
  Result := '';
  if RegQueryStringValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run',
                         'ClipMonster Automation', Cmd) and (Copy(Cmd, 1, 1) = '"') then
  begin
    Delete(Cmd, 1, 1);
    P := Pos('"', Cmd);
    if P > 0 then
      Result := ExtractFileDir(Copy(Cmd, 1, P - 1));
  end;
end;

{ Ask automation to stop and wait for it (`ytclip auto --stop --wait`), so it puts the video it's on
  back in the queue instead of being cut off. Exit code 10: it was running, and stopped. }
function StopAutomation(const Folder: String): Boolean;
var
  Code: Integer;
begin
  Result := False;
  if FileExists(Folder + '\ytclip.exe') then
    if Exec(Folder + '\ytclip.exe', 'auto --stop --wait', '', SW_HIDE, ewWaitUntilTerminated, Code) then
      Result := Code = 10;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  WasRunning := StopAutomation(AutomationFolder);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    StopAutomation(ExpandConstant('{app}'));
    { Start with Windows (pipeline.RUN_VALUE) would point at a program that's gone. }
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'ClipMonster Automation');
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    if SuppressibleMsgBox('Also delete ClipMonster''s data from this PC?' + #13#10#13#10 +
              'That is your settings, niches, automation queue and history, the YouTube and Google Drive ' +
              'sign-ins kept by the app, and its downloads, models and updates. The clips you made are not ' +
              'touched.' + #13#10#13#10 + 'Choose No to keep it for a later reinstall.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
    begin
      DelTree(ExpandConstant('{userappdata}\ClipMonster'), True, True, True);
      DelTree(ExpandConstant('{localappdata}\ClipMonster'), True, True, True);
    end;
  end;
end;
