# Claude PR 자동 리뷰

PR이 열리거나 갱신되거나 초안(Draft)에서 해제될 때마다 Claude Agent SDK가 diff를 리뷰해 요약 코멘트 1개와 변경 라인 인라인 코멘트를 남깁니다.
리뷰는 코멘트로만 남으며 머지를 막지 않습니다.

> 공개 저장소(`bullbearbull/AutomaticETL`)에서도 안전하게 동작하도록 `pull_request` 이벤트만 사용하고 `pull_request_target`은 금지하며, 외부 fork PR은 `head.repo.full_name == github.repository` 조건으로 자동 건너뜁니다(fork에는 `ANTHROPIC_API_KEY` 시크릿이 전달되지 않습니다).

## 설정
1. 저장소 Settings → Secrets and variables → Actions에 `ANTHROPIC_API_KEY`를 등록합니다.
   - CLI 예시: `gh secret set ANTHROPIC_API_KEY -R bullbearbull/AutomaticETL`
   - 시크릿이 아직 등록되지 않은 경우 워크플로는 실패 코멘트 없이 정상 종료(건너뜀)합니다.
2. `.github/pr-review/`와 `.github/workflows/code-review.yml`이 이 저장소의 리뷰 구성입니다.
3. 자동 리뷰는 PR 기준 커밋의 `.github/pr-review/scripts/review_prompt.md`에서 리뷰 규칙을 읽습니다. `--diff-file`로 로컬에서 실행하면 스크립트와 함께 제공된 규칙을 사용합니다.

## 환경 변수
| 이름 | 기본값 | 설명 |
|---|---|---|
| `REVIEW_SYSTEM_PROMPT` | 기준 커밋의 리뷰 규칙 | 실행 환경에서 시스템 프롬프트를 지정할 때 사용 |
| `REVIEW_MODEL` | `claude-opus-5-5` | 사용할 모델 |
| `REVIEW_MAX_TURNS` | `20` | 에이전트 최대 턴 |
| `REVIEW_MAX_BUDGET_USD` | `2.0` | 실행당 비용 상한(클라이언트 측 추정치) |
| `REVIEW_MAX_DIFF_CHARS` | `120000` | 이보다 큰 diff는 뒤쪽 파일을 생략하고 요약에 표시 |
| `REVIEW_TIMEOUT_SECONDS` | `720.0` | 에이전트 실행 타임아웃(초) |
| `REVIEW_BOT_LOGIN` | `github-actions[bot]` | 요약·인라인 코멘트 소유자로 인정할 봇 계정 로그인 |

## 로컬 테스트
`.github/pr-review/tests`는 이 저장소의 `pytest`(`tests/`만 수집)와 별개이며 CI에서 실행되지 않습니다.
```bash
# 단위 테스트(모델 호출 없음)
uv run --no-project --python 3.12 --with-requirements .github/pr-review/scripts/requirements.txt --with pytest \
  pytest .github/pr-review/tests -p no:cacheprovider

# 실제 호출(크레딧 소모). GitHub에는 게시하지 않고 출력만 합니다.
git diff main...HEAD > /tmp/pr.diff
ANTHROPIC_API_KEY=... uv run --no-project --python 3.12 --with-requirements .github/pr-review/scripts/requirements.txt \
  python .github/pr-review/scripts/review.py --diff-file /tmp/pr.diff
```

## 동작과 제약
- 에이전트 도구는 `Read`, `Grep`, `Glob`만 허용하고 `.env`·키 파일·`/proc`·`/sys`·홈 숨김 경로(`~/.*`, `~/.*/**`)는 읽기·검색을 거부합니다. 추가로 `PreToolUse` 훅(`on_failure="block"`)이 저장소 작업 디렉터리 외부 경로 및 심볼릭 링크 이탈을 원천 차단합니다.
- 리뷰 작업 공간에는 크기 제한을 적용한 diff에 포함된 Git 추적 파일 중 비민감 일반 파일만 복사합니다.
- 모델 요약과 지적에 링크, 멘션, 이슈 참조 또는 HTML이 포함되면 리뷰 내용 대신 실패 안내를 게시합니다.
- fork PR, draft PR, dependabot PR, 문서만 바뀐 PR은 건너뜁니다. `pull_request_target`은 사용하지 않습니다.
- 같은 커밋 SHA는 다시 리뷰하지 않습니다. 실패한 리뷰는 재실행하면 다시 시도하며, 이전 성공 요약이 이미 있으면 덮어쓰지 않고 하단에 실패 안내만 덧붙입니다.
- 요약·인라인 코멘트는 지정 봇 계정(`github-actions[bot]`) 작성분만 매칭해 타 계정·타 봇의 마커 스푸핑을 차단하며, GitHub 65,536자 코멘트 길이 상한을 넘지 않도록 안전하게 절단합니다.
- GitHub API가 diff를 거부하는 아주 큰 PR(약 300개 파일 초과)은 체크아웃된 로컬 저장소의 `git diff base...head`로 자동 폴백해 `REVIEW_MAX_DIFF_CHARS` 한도 내에서 리뷰합니다.
- 어떤 오류가 나도 job은 성공(exit 0)으로 끝나고, 가능하면 실패 안내 코멘트를 남깁니다.
- 각 실행의 토큰·캐시 토큰·추정 비용은 job summary에서 확인합니다. `cache read tokens`가 0이 아니면 프롬프트 캐싱이 적용된 것입니다.
- 워크플로는 PR 브랜치의 `.github/pr-review/scripts/`를 실행하므로, 저장소에 쓰기 권한이 있는 사람이 스크립트를 바꿔 API 키를 읽을 수 있습니다.
  신뢰 경계가 중요하면 스크립트를 별도 저장소의 재사용 워크플로로 옮기세요.
