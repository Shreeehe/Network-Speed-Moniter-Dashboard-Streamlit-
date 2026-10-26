' Silent background runner for Network and Power Monitor
Set WshShell = CreateObject("WScript.Shell")
strPath = WScript.ScriptFullName
Set objFSO = CreateObject("Scripting.FileSystemObject")
Set objFile = objFSO.GetFile(strPath)
strFolder = objFSO.GetParentFolderName(objFile)
WshShell.CurrentDirectory = strFolder
WshShell.Run Chr(34) & strFolder & "\start_monitor.bat" & Chr(34), 0, False
Set WshShell = Nothing
