Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
venvPythonw = root & "\.venv\Scripts\pythonw.exe"
appPath = root & "\app.pyw"

If fso.FileExists(venvPythonw) Then
    pythonwPath = venvPythonw
Else
    pythonwPath = "pythonw"
End If

shell.CurrentDirectory = root
shell.Run """" & pythonwPath & """ """ & appPath & """", 0, False
