; -*- coding: utf-8 -*-
; 本地大模型监控面板 - Windows 一键安装包（Inno Setup 6 中文向导）
;
; 安装流程：
;   1. 解压面板主程序到用户目录（不需要管理员权限，不弹 UAC）
;   2. 自定义页收集 SSH 地址 / 端口 / 用户名 / 密码
;   3. 自动调用 deploy.exe 完成远端部署，每一步实时显示在进度页
;   4. 生成开始菜单与桌面快捷方式（桌面图标默认就装，不再作为可选项）
;
; 所有子进程都以隐藏方式启动，安装与运行过程中不会出现黑色控制台窗口。
; Pascal 脚本里的标识符只能用 ASCII，所以变量名统一用英文。

#define MyAppName "本地大模型监控面板"
#define MyAppShort "llama-panel"
#define MyVersion "1.0.0"
#define MyPublisher "lmq9622"

[Setup]
AppId={{7E3C9A54-2B41-4C88-9E5D-6F1A8B0C2D31}
AppName={#MyAppName}
AppVersion={#MyVersion}
AppVerName={#MyAppName} {#MyVersion}
AppPublisher={#MyPublisher}
DefaultDirName={autopf}\{#MyAppShort}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=llama-panel-setup-{#MyVersion}
; 安装程序自己用 llama 图标，任务栏与文件属性里都能认出来
SetupIconFile=..\assets\llama.ico
Compression=lzma2/max
SolidCompression=yes
UninstallDisplayIcon={app}\llama-monitor-panel.exe
UninstallDisplayName={#MyAppName}
AllowNoIcons=yes

[Languages]
Name: "chinese"; MessagesFile: "ChineseSimplified.isl"

[Files]
Source: "..\dist\llama-monitor-panel.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\deploy.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\llama-monitor-panel.exe"; IconFilename: "{app}\llama-monitor-panel.exe"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
; 注意：这里不带 Tasks 限定，桌面快捷方式一定创建（以前的 desktopicon 任务在静默安装下不会被勾选）
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\llama-monitor-panel.exe"; IconFilename: "{app}\llama-monitor-panel.exe"

[Run]
Filename: "{app}\llama-monitor-panel.exe"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\panel-data"

[Code]
var
  ServerPage: TInputQueryWizardPage;
  ProgressPage: TOutputProgressWizardPage;
  DeployOk: Boolean;
  DeployMessage: String;
  SkipDeploy: Boolean;

const
  DEPLOY_TIMEOUT_MS = 300000;
  PROGRESS_STEPS = 8;

procedure InitializeWizard;
begin
  ServerPage := CreateInputQueryPage(wpSelectTasks,
    '服务器连接信息',
    '自动部署远端 llama.cpp 监控代理',
    '请填写要部署的 Linux 服务器信息。安装程序会通过 SSH 自动上传监控代理、写入配置并重启服务，' +
    '无需手工登录服务器。' + #13#10 + #13#10 +
    '只想先在本机安装面板（稍后再填服务器）时，把「服务器地址」留空即可。');
  ServerPage.Add('服务器地址（IP 或域名）:', False);
  ServerPage.Add('SSH 端口:', False);
  ServerPage.Add('登录用户名（同时用于 sudo）:', False);
  ServerPage.Add('登录密码:', True);
  ServerPage.Add('sudo 密码（留空 = 与登录密码相同）:', True);
  ServerPage.Add('远端安装目录（留空 = 用户主目录）:', False);
  ServerPage.Values[1] := '22';

  ProgressPage := CreateOutputProgressPage('正在自动安装远端服务',
    '正在通过 SSH 部署监控代理，请勿关闭安装程序。');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Port: Integer;
begin
  Result := True;
  if CurPageID <> ServerPage.ID then
    exit;

  if ServerPage.Values[0] = '' then begin
    SkipDeploy := True;
    exit;
  end;
  SkipDeploy := False;

  if ServerPage.Values[2] = '' then begin
    { 用 SuppressibleMsgBox 而不是 MsgBox：
      只有前者才会遵守命令行 /SUPPRESSMSGBOXES，
      否则静默安装（/SILENT）会一直停在弹框上等点击，永远不退出。 }
    SuppressibleMsgBox('请填写登录用户名。', mbError, MB_OK, IDOK);
    Result := False;
    exit;
  end;
  Port := StrToIntDef(ServerPage.Values[1], 0);
  if (Port < 1) or (Port > 65535) then begin
    SuppressibleMsgBox('SSH 端口必须是 1 到 65535 之间的数字。', mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

{ 读取进度文件的行数 }
function LineCount(const FileName: String): Integer;
var
  Lines: TArrayOfString;
begin
  Result := 0;
  if LoadStringsFromFile(FileName, Lines) then
    Result := GetArrayLength(Lines);
end;

{ 读取进度文件的最后一行，用来显示当前步骤 }
function LastLine(const FileName: String): String;
var
  Lines: TArrayOfString;
  N: Integer;
begin
  Result := '';
  if not FileExists(FileName) then
    exit;
  if not LoadStringsFromFile(FileName, Lines) then
    exit;
  N := GetArrayLength(Lines);
  if N > 0 then
    Result := Lines[N - 1];
end;

{ 转义成 JSON 字符串字面量的内容 }
function JsonEscape(const S: String): String;
var
  I: Integer;
  C: Char;
begin
  Result := '';
  for I := 1 to Length(S) do begin
    C := S[I];
    if C = '\' then
      Result := Result + '\\'
    else if C = '"' then
      Result := Result + '\"'
    else if (C = #13) or (C = #10) then
      Result := Result + '\n'
    else
      Result := Result + C;
  end;
end;

{ 读文件末尾若干行：部署失败时把日志摘要放进提示框 }
function TailLines(const FileName: String; MaxLines: Integer): String;
var
  Lines: TArrayOfString;
  I, N: Integer;
begin
  Result := '';
  if not FileExists(FileName) then
    exit;
  if not LoadStringsFromFile(FileName, Lines) then
    exit;
  N := GetArrayLength(Lines);
  I := N - MaxLines;
  if I < 0 then
    I := 0;
  while I < N do begin
    Result := Result + Lines[I] + #13#10;
    I := I + 1;
  end;
end;

{ 文件里是否含有某个片段（用来兜底判断部署结果） }
function FileContains(const FileName, Needle: String): Boolean;
var
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := False;
  if not LoadStringsFromFile(FileName, Lines) then
    exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
    if Pos(Needle, Lines[I]) > 0 then begin
      Result := True;
      exit;
    end;
end;

procedure RunDeploy;
var
  ResultCode: Integer;
  Params: String;
  ArgsFile: String;
  ProgressFile: String;
  ResultFile: String;
  LogFile: String;
  Json: String;
  Last: String;
  Elapsed: Integer;
  N: Integer;
begin
  DeployOk := False;
  DeployMessage := '';
  ArgsFile := ExpandConstant('{tmp}\deploy-args.json');
  ProgressFile := ExpandConstant('{tmp}\deploy-progress.txt');
  ResultFile := ExpandConstant('{tmp}\deploy-result.json');
  LogFile := ExpandConstant('{tmp}\deploy-log.txt');
  DeleteFile(ArgsFile);
  DeleteFile(ProgressFile);
  DeleteFile(ResultFile);
  DeleteFile(LogFile);

  { 参数先写成 JSON 文件再交给 deploy.exe。
    直接在命令行里拼字符串时，密码留空会拼出「--password --remote-dir /home/lmq」，
    argparse 会把 --remote-dir 当成密码值然后立刻报错退出，
    表现为 Exec 返回成功但进程秒退、进度页永远转圈。
    走文件之后，密码里的空格、引号、反斜杠都不会再出问题。 }
  Json :=
    '{' + #13#10 +
    '  "host": "' + JsonEscape(ServerPage.Values[0]) + '",' + #13#10 +
    '  "port": ' + IntToStr(StrToIntDef(ServerPage.Values[1], 22)) + ',' + #13#10 +
    '  "user": "' + JsonEscape(ServerPage.Values[2]) + '",' + #13#10 +
    '  "password": "' + JsonEscape(ServerPage.Values[3]) + '",' + #13#10 +
    '  "sudo_password": "' + JsonEscape(ServerPage.Values[4]) + '",' + #13#10 +
    '  "remote_dir": "' + JsonEscape(ServerPage.Values[5]) + '",' + #13#10 +
    '  "app_dir": "' + JsonEscape(ExpandConstant('{app}')) + '",' + #13#10 +
    '  "progress": "' + JsonEscape(ProgressFile) + '",' + #13#10 +
    '  "result": "' + JsonEscape(ResultFile) + '",' + #13#10 +
    '  "log": "' + JsonEscape(LogFile) + '"' + #13#10 +
    '}';
  if not SaveStringToFile(ArgsFile, Json, False) then begin
    DeployMessage := '无法写入临时参数文件：' + ArgsFile;
    exit;
  end;

  Params := '--args-file ' + AddQuotes(ArgsFile);

  { SW_HIDE + ewNoWait：隐藏窗口启动，脚本这边轮询进度文件 }
  if not Exec(ExpandConstant('{app}\deploy.exe'), Params, '', SW_HIDE, ewNoWait, ResultCode) then begin
    DeployMessage := '无法启动自动安装程序 deploy.exe。';
    exit;
  end;

  ProgressPage.SetText('正在连接服务器并自动部署…', '');
  ProgressPage.SetProgress(0, PROGRESS_STEPS);
  ProgressPage.Show;
  try
    Elapsed := 0;
    Last := '';
    repeat
      Sleep(250);
      Elapsed := Elapsed + 250;
      N := LineCount(ProgressFile);
      if N > 0 then begin
        Last := LastLine(ProgressFile);
        if N > PROGRESS_STEPS then
          N := PROGRESS_STEPS;
        ProgressPage.SetProgress(N, PROGRESS_STEPS);
        ProgressPage.SetText(Last, '');
      end;
    until FileExists(ResultFile) or (Elapsed >= DEPLOY_TIMEOUT_MS);

    { 等到结果文件出现时，进度文件的最后一行可能还差几十毫秒才落盘，等一下再读 }
    Sleep(400);
    if not FileExists(ResultFile) then begin
      DeployMessage := '等待超时（5 分钟）。' + #13#10 + '最后一步：' + Last;
      exit;
    end;

    Last := LastLine(ProgressFile);
    { 注意：这里必须用 Pos 判断整串。
      以前写的是 Copy(Last, 1, 4) = '结果: OK'，左边只有 4 个字符（「结果: 」），
      右边 6 个字符，永远不相等，导致明明部署成功也被判成失败。 }
    if (Pos('结果: OK', Last) = 1) or FileContains(ResultFile, '"ok": true') then begin
      DeployOk := True;
      DeployMessage := Last;
      ProgressPage.SetProgress(PROGRESS_STEPS, PROGRESS_STEPS);
    end else begin
      DeployMessage := Last + #13#10 + #13#10 +
        '详细日志：' + LogFile + #13#10 + #13#10 +
        TailLines(LogFile, 8);
    end;
  finally
    ProgressPage.Hide;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    exit;
  if SkipDeploy or (ServerPage.Values[0] = '') then
    exit;

  RunDeploy;

  if DeployOk then begin
    SuppressibleMsgBox('远端自动安装完成。' + #13#10 + #13#10 +
           '面板已经连接好服务器，双击桌面图标即可查看实时监控与对话流终端。',
           mbInformation, MB_OK, IDOK);
  end else begin
    SuppressibleMsgBox('远端自动安装未完成：' + #13#10 + #13#10 + DeployMessage + #13#10 + #13#10 +
           '面板本身已经装好，可以打开后在「设置」里改服务器地址再重连。',
           mbError, MB_OK, IDOK);
  end;
end;
