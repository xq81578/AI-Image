Option Explicit

' Use the script directory so Chinese workspace paths are handled by the
' Windows Unicode filesystem APIs instead of being assembled by cmd.exe.
Dim shell, fileSystem, scriptDirectory, pythonwPath, serverPath, quote
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
scriptDirectory = fileSystem.GetParentFolderName(WScript.ScriptFullName)
pythonwPath = "C:\Python313\pythonw.exe"
serverPath = fileSystem.BuildPath(scriptDirectory, "server.py")
quote = Chr(34)

shell.CurrentDirectory = scriptDirectory
shell.Run quote & pythonwPath & quote & " " & quote & serverPath & quote, 0, False
