"""시간표 편집기.

- model: 순수 로직(수업·공휴일 모델, config.toml 읽기/쓰기, Swift 앱 JSON 호환)
- gui:   PySide6 편집기 (`python -m syncnote timetable edit`)

GUI 는 PySide6 가 있을 때만 필요합니다. model 은 표준 라이브러리만 씁니다.
"""
