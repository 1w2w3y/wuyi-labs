Option Explicit

Dim shell, command, index

If WScript.Arguments.Count = 0 Then
    WScript.Quit 64
End If

command = QuoteArgument(WScript.Arguments(0))
For index = 1 To WScript.Arguments.Count - 1
    command = command & " " & QuoteArgument(WScript.Arguments(index))
Next

Set shell = CreateObject("WScript.Shell")
WScript.Quit shell.Run(command, 0, True)

Function QuoteArgument(value)
    Dim text
    text = CStr(value)
    If InStr(text, Chr(34)) > 0 Then
        WScript.Quit 65
    End If
    QuoteArgument = Chr(34) & text & Chr(34)
End Function
