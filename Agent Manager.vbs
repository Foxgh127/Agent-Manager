Option Explicit

Dim fso, shell, appDir, scriptPath, launcher, command, checkCode
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = fso.BuildPath(appDir, "agent_manager_app.py")

checkCode = shell.Run("cmd /c where pyw >nul 2>&1", 0, True)
If checkCode = 0 Then
  launcher = "pyw -3"
Else
  checkCode = shell.Run("cmd /c where pythonw >nul 2>&1", 0, True)
  If checkCode <> 0 Then
    MsgBox "未找到 Python 3。请安装 Python 后重试，或直接使用 release 目录中的 EXE。", 16, "Agent Manager"
    WScript.Quit 1
  End If
  launcher = "pythonw"
End If

command = launcher & " """ & scriptPath & """"
shell.Run command, 0, False
