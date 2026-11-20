"""실시간 오디오 송출 (Tailscale 망 안에서만).

  capture.py  소리 받기 -> ffmpeg 로 mp3/aac 인코딩
  hub.py      내장 HTTP 오디오 서버 (여러 청취자에게 나눠 보내기)
  tailnet.py  Tailscale 상태 읽기, serve/funnel 켜고 끄기, 링크 만들기
  notify.py   링크가 생길 때마다 GitHub 이슈로 올리기
  live.py     위를 묶어 한 세션을 돌리기
"""
