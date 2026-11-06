; Inno Setup script for the Windows installer (see packaging/make-installer.sh).
;
; Installs per user into %LOCALAPPDATA%\Programs\Curslink, so neither
; installing nor updating needs admin rights, and adds that folder to the
; user's PATH. `curslink update` (or the automatic update when the host or an
; agent starts) re-runs a newer setup silently over the top.
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
UninstallDisplayIcon={app}\curslink.exe
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

[Icons]
Name: "{autoprograms}\Curslink"; Filename: "{cmd}"; Parameters: "/k ""cd /d %USERPROFILE% && curslink --help"""; WorkingDir: "{app}"; Comment: "Open a command prompt for Curslink"

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
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;
