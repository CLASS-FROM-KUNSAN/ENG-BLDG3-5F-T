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
'    (데이터 폴더의 logs\syncnote.log — 위치는 doctor.bat 이 알려 줍니다)
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

' 두 번째 인자 0 = 창 숨김,  세 번째 True = 끝날 때까지 기다림
'
' 기다리지 않으면 작업 스케줄러가 곧바로 '성공' 으로 보고 끝냅니다.
' 그러면 register_tasks.ps1 의
'     -RestartCount 2 -RestartInterval 5분
'     -ExecutionTimeLimit 12시간
' 이 전부 무용지물입니다 — 재시작할 '실패' 가 없고, 시간 제한은 이미
' 끝난 wscript 에 걸립니다. 수업 중에 파이썬이 죽어도 스케줄러는
' 모르고, 다음 로그온까지 그날 수업이 통째로 빕니다.
' .bat 의 종료 코드를 그대로 돌려줘야 스케줄러가 실패를 알아봅니다.
Dim rc
rc = shell.Run(cmd, 0, True)
WScript.Quit rc
