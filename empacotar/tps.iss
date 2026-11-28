; Instalador do TPS (Inno Setup 6). Gerado no GitHub Actions; à mão no Windows:
;   python empacotar\compilar.py   e depois   iscc /DVersao=0.9.10 empacotar\tps.iss
;
; Programa → C:\Program Files\TPS (substituído a cada atualização)
; Dados    → C:\ProgramData\TPS (usuários, auditoria, planos, exportações). Nunca é apagada pelo instalador.

#ifndef Versao
  #define Versao "0.9.10"
#endif

[Setup]
AppId={{3B8E2D41-6C7A-4F19-9E52-A1D04C7B8F63}
AppName=TPS
AppVersion={#Versao}
AppVerName=TPS {#Versao}
AppPublisher=TPS
DefaultDirName={autopf}\TPS
DefaultGroupName=TPS
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=saida
OutputBaseFilename=TPS-Setup-{#Versao}
SetupIconFile=tps.ico
UninstallDisplayIcon={app}\TPS.exe
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=2
WizardStyle=modern
CloseApplications=force
UsePreviousAppDir=yes

[Languages]
Name: "pt"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Messages]
pt.WelcomeLabel2=Este programa vai instalar o TPS {#Versao} (planejamento de radioterapia).%n%nVERSÃO EM DESENVOLVIMENTO: não usar para tratar pacientes.

[Tasks]
Name: "atalho"; Description: "Criar atalho na Área de Trabalho"; GroupDescription: "Atalhos:"

[Files]
Source: "saida\TPS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "ia\instalar_ia.ps1"; DestDir: "{app}\ia"; Flags: ignoreversion

[Dirs]
Name: "{commonappdata}\TPS"; Permissions: users-modify; Flags: uninsneveruninstall

[Icons]
Name: "{group}\TPS"; Filename: "{app}\TPS.exe"; IconFilename: "{app}\tps.ico"
Name: "{group}\TPS no navegador"; Filename: "{app}\TPS-cmd.exe"; Parameters: "--navegador"; IconFilename: "{app}\tps.ico"
Name: "{group}\Gerar casos de teste (Documentos)"; Filename: "{app}\TPS-cmd.exe"; Parameters: "--casos-teste ""{userdocs}\TPS casos de teste"""; IconFilename: "{app}\tps.ico"
Name: "{group}\Pasta de dados do TPS"; Filename: "{commonappdata}\TPS"
Name: "{group}\Instalar segmentação automática"; Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -NoExit -File ""{app}\ia\instalar_ia.ps1"""
Name: "{group}\Desinstalar o TPS"; Filename: "{uninstallexe}"
Name: "{autodesktop}\TPS"; Filename: "{app}\TPS.exe"; IconFilename: "{app}\tps.ico"; Tasks: atalho

[Run]
Filename: "{app}\TPS-cmd.exe"; Parameters: "--casos-teste ""{userdocs}\TPS casos de teste"""; Flags: runhidden runasoriginaluser; StatusMsg: "Gerando casos de teste…"
Filename: "{app}\TPS.exe"; Description: "Abrir o TPS agora"; Flags: postinstall nowait runasoriginaluser skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM TPS.exe"; Flags: runhidden waituntilterminated; RunOnceId: "fechar"
