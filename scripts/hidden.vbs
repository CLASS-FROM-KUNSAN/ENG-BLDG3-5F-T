'=====================================================================
'  창을 띄우지 않고 .bat 을 실행합니다.
'
'  왜 필요한가:
'    작업 스케줄러에서 .bat 을 직접 실행하면 "숨김" 으로 설정해도
'    검은 콘솔 창이 한순간 깜빡입니다. 수업 중에 이게 뜨면 거슬립니다.
'    .vbs 를 거치면 창이 아예 생기지 않습니다.
'
'  사용법:
'    wscript.exe hidden.vbs "C:\...\scripts\eclass_sync.bat"
'    wscript.exe hidden.vbs "C:\...\scripts\record_schedule.bat" "--once"
'
'  주의:
'    창이 없으니 오류도 안 보입니다. 반드시 로그 파일을 확인하세요.
'    (데이터 폴더의 logs\gymhall.log — 위치는 doctor.bat 이 알려 줍니다)
'=====================================================================
Option Explicit

Dim shell, args, cmd, i
Set shell = CreateObject("WScript.Shell")
Set args = WScript.Arguments

If args.Count = 0 Then
    MsgBox "실행할 .bat 파일 경로를 인자로 주세요." & vbCrLf & vbCrLf & _
           "예: wscript.exe hidden.vbs ""C:\path\scripts\eclass_sync.bat""", _
           vbExclamation, "hidden.vbs"
    WScript.Quit 1
End If

cmd = """" & args(0) & """"
For i = 1 To args.Count - 1
    cmd = cmd & " """ & args(i) & """"
Next

' 두 번째 인자 0 = 창 숨김,  세 번째 False = 끝날 때까지 기다리지 않음
shell.Run cmd, 0, False
WScript.Quit 0
