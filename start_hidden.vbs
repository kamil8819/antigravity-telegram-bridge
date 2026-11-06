Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
curDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = curDir
sh.Run "pythonw.exe """ & curDir & "\supervisor.py""", 0, False
