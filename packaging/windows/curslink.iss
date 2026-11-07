; Inno Setup script for the Windows installer (see packaging/make-installer.sh).
;
; Installs per user into %LOCALAPPDATA%\Programs\Curslink, so neither
; installing nor updating needs admin rights, and adds that folder to the
; user's PATH. `curslink update` (or the automatic update when the host, an
; agent or the app starts) re-runs a newer setup silently over the top.
;
; The Start menu's "Curslink" opens the app (curslinkw.exe: no console
; window), which then lives in the notification area.
;
;   iscc /DAppVersion=0.2.0 /DAppNumVersion=0.2.0 /DSourceDir=...\curslink /DOutputDir=out curslink.iss

#ifndef AppVersion
  #error Pass /DAppVersion=x.y.z
#endif
#ifndef AppNumVersion
  #define AppNumVersion AppVersion
#endif

[Setup]
; Never change AppId: it's how a newer setup finds the installed copy.
AppId={{6F0B3C2E-6A51-4C4B-9D7E-6A8C0C5E2B71}
AppName=Curslink
AppVersion={#AppVersion}
AppVerName=Curslink {#AppVersion}
VersionInfoVersion={#AppNumVersion}
AppPublisher=Curslink
AppPublisherURL=https://github.com/kylepantall/Curslink
AppUpdatesURL=https://github.com/kylepantall/Curslink/releases
DefaultDirName={autopf}\Curslink
DisableDirPage=auto
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
CloseApplications=force
RestartApplications=no
UninstallDisplayIcon={app}\curslinkw.exe
SetupIconFile=..\icons\curslink.ico
UninstallDisplayName=Curslink
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
OutputDir={#OutputDir}
OutputBaseFilename=curslink-{#AppVersion}-windows-x64-setup

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Drop the old version's libraries rather than leaving stale ones behind.
Type: filesandordirs; Name: "{app}\_internal"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Tasks]
Name: "desktopicon"; Description: "Put a Curslink shortcut on the desktop"; Flags: unchecked

[Icons]
Name: "{autoprograms}\Curslink"; Filename: "{app}\curslinkw.exe"; WorkingDir: "{app}"; Comment: "Look at a device and your mouse and keyboard follow"
Name: "{autoprograms}\Curslink command prompt"; Filename: "{cmd}"; Parameters: "/k ""cd /d %USERPROFILE% && curslink --help"""; WorkingDir: "{app}"; IconFilename: "{app}\curslink.exe"; Comment: "Open a command prompt for Curslink"
Name: "{autodesktop}\Curslink"; Filename: "{app}\curslinkw.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Not after a silent install: an update restarts Curslink by itself.
Filename: "{app}\curslinkw.exe"; Description: "Open Curslink"; Flags: nowait postinstall skipifsilent

[Registry]
; Start at sign-in is switched on from the app (curslink/autostart.py);
; uninstalling switches it off.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "Curslink"; Flags: uninsdeletevalue dontcreatekey

[Code]
const
  EnvKey = 'Environment';

function PathHas(Dir: string): Boolean;
var
  Paths: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Paths) then
    Result := False
  else
    Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') > 0;
end;

procedure AddToPath(Dir: string);
var
  Paths: string;
begin
  if PathHas(Dir) then
    exit;
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Paths) then
    Paths := '';
  if (Paths <> '') and (Copy(Paths, Length(Paths), 1) <> ';') then
    Paths := Paths + ';';
  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Paths + Dir);
end;

procedure RemoveFromPath(Dir: string);
var
  Paths: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Paths) then
    exit;
  Paths := ';' + Paths + ';';
  P := Pos(';' + Uppercase(Dir) + ';', Uppercase(Paths));
  if P = 0 then
    exit;
  Delete(Paths, P, Length(Dir) + 1);
  Paths := Copy(Paths, 2, Length(Paths) - 2);
  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Paths);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    AddToPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
begin
  // The input helper (Settings > Control admin apps and UAC prompts) is a
  // service in Program Files: removing it takes admin, so ask.
  if (CurUninstallStep = usUninstall) and
     Exec(ExpandConstant('{sys}\sc.exe'), 'query CurslinkHelper', '', SW_HIDE, ewWaitUntilTerminated, Code) and
     (Code = 0) then
    ShellExec('runas', ExpandConstant('{app}\curslink.exe'), 'winhelper uninstall', '', SW_HIDE,
              ewWaitUntilTerminated, Code);
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;
