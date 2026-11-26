# 기여 안내

관심 가져 주셔서 감사합니다. 버그 제보, 문서 개선, 기능 제안 모두 환영합니다.

## 시작하기

- 버그는 재현 방법과 실행 환경(OS, Python 버전, autoetl 버전)을 적어 이슈로 남겨 주세요.
- 큰 기능 변경은 PR을 만들기 전에 이슈에서 먼저 방향을 맞추면 좋습니다.

## 개발 환경과 검증

Python 3.10 이상과 [uv](https://docs.astral.sh/uv/)를 사용합니다.

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run python tools/license_check.py
```

PR은 Windows·macOS·Linux × Python 3.10~3.13 CI 전 항목 통과를 기준으로 병합합니다.

웹 UI(`frontend/`)를 고칠 때는 [frontend/README.md](frontend/README.md)의 개발 안내를 따라 주세요.

## 기본 규칙

- **의존성**: 핵심 런타임 의존성은 허용형 라이선스(MIT·BSD·Apache-2.0·PSF·MPL-2.0 계열)만 사용하며, 새 의존성을 추가할 때는 PR에 사유와 라이선스를 명시해 주세요.
- **개인정보·실데이터 금지**: 실제 문서 원문, API 키, 개인정보를 이슈·PR·테스트 픽스처에 넣지 않습니다.
- **크로스 플랫폼**: Windows·macOS·Linux에서 동일하게 동작하도록 경로는 `pathlib`, 파일 인코딩은 UTF-8을 기본으로 사용합니다.

## 행동 강령

[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)를 따릅니다.
