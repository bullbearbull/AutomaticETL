# AutoETL 새 화면 (frontend)

`autoetl ui`가 `/next/` 주소로 보여 주는 새 화면의 소스다. React 19, TypeScript, Vite로 만들고, 탭·대화상자처럼 동작이 필요한 부품은 React Aria Components를 쓴다. 빌드 결과는 `../src/autoetl/ui/static/`에 써서 파이썬 패키지와 함께 배포한다. 지금 화면(`/`)은 바뀌지 않는다.

## 준비

- Node 버전은 `.nvmrc`에 적힌 버전(22.23.5)을 쓴다. nvm·fnm·Volta 같은 도구가 이 파일을 읽는다. 다른 버전을 쓴다면 22.18 이상이어야 `npm run test:unit`이 돈다(그 전 버전은 `.ts` 파일을 바로 실행하지 못한다).
- 의존성은 늘 잠금 파일 그대로 설치한다.

```bash
cd frontend
npm ci
```

`npm install`은 잠금 파일을 바꿀 수 있으니 새 패키지를 넣을 때만 쓴다. `.npmrc`의 `save-exact=true` 때문에 버전은 정확한 값으로 적힌다.

## 명령

| 명령 | 하는 일 |
| -- | -- |
| `npm run dev` | 개발 서버를 띄운다. 주소는 `http://localhost:5173/next/`다. |
| `npm run typecheck` | `tsc -b`로 앱·설정·테스트 코드의 타입을 검사한다. |
| `npm run test:unit` | `tests/unit/*.test.ts`를 `node --test`로 돌린다. Node의 타입 제거 기능을 써서 따로 설치할 것이 없다. |
| `npm run build` | `../src/autoetl/ui/static/`을 비우고 새로 빌드한다. |
| `npm run test:e2e` | Playwright로 화면 조작 테스트와 axe 접근성 검사를 돌린다. |

개발 서버는 `/api` 요청을 `http://127.0.0.1:8765`로 넘긴다. 다른 창에서 `uv run autoetl ui --no-browser`로 로컬 서버를 먼저 띄운다. 로컬 서버는 `Origin`이 `http://<Host>`와 다른 POST를 거절하므로, 프록시가 `Host`와 함께 `Origin`도 `http://127.0.0.1:8765`로 바꿔 보낸다(`vite.config.ts`).

빌드 결과를 실제 서버로 확인하려면 `npm run build` 뒤에 `uv run autoetl ui`를 실행하고 `http://127.0.0.1:8765/next/`를 연다.

`test:e2e`는 저장소 루트에서 `uv run --frozen autoetl ui --port 8799 --no-browser`로 실제 서버를 띄우고, 그 서버가 주는 빌드 결과(`/next/`)를 Chromium으로 조작한다. 그래서 먼저 저장소 루트에서 `uv sync --frozen`을, 이 폴더에서 `npm run build`를 실행해 둔다. 로컬에서는 8799 포트에 이미 떠 있는 서버를 그대로 쓴다. Playwright 브라우저는 처음 한 번 `npx playwright install chromium`으로 설치한다. 실패하면 `playwright-report/`에 보고서가 남는다(`npx playwright show-report`). `@playwright/test`와 `playwright-core`는 같은 버전으로 함께 올린다(`@axe-core/playwright`가 `playwright-core`의 타입을 쓰기 때문이다).

## 폴더 구성

```
frontend/
  index.html              <html lang="ko">, 외부 자원·인라인 스크립트 없음
  vite.config.ts          base /next/, 출력 위치, 고정 파일 이름, 개발 프록시
  public/                 그대로 복사되는 파일(favicon.svg)
  src/
    app/                  App.tsx(위쪽 줄·탭 목록), TabContent.tsx, main.tsx [공유]
    styles/               tokens.css(디자인 토큰), base.css              [공유]
    components/           공유 부품. components/index.ts에서 가져온다   [공유]
    copy/common.ts        여러 탭이 함께 쓰는 문구                        [공유]
    api/http.ts           getJson·postJson                               [공유]
    lib/                  markdown.ts, tableClipboard.ts, files.ts,
                          errors.ts, korean.ts (React 없는 순수 TS)        [공유]
    features/
      extract/            문서 글·표 추출 탭 (ExtractTab.tsx)
      database/           엑셀·CSV로 DB 만들기 탭 (DatabaseTab.tsx)
      search/             문서 내용 검색 탭 (SearchTab.tsx)
      settings/           설정 탭 (SettingsTab.tsx)
  tests/unit/             공유 lib·api의 단위 테스트
  e2e/                    Playwright 테스트
```

### 탭을 고칠 때 건드리는 곳

- 탭 하나의 작업은 그 탭의 폴더 `src/features/<탭>/`만 고친다. 서버와 주고받는 요청·응답 타입은 그 폴더의 `api.ts`에, 그 탭에서만 쓰는 문구는 `copy.ts`에 둔다. 탭의 테스트와 다시 빌드한 결과물(`src/autoetl/ui/static/`)도 같은 PR에 넣는다.
- 탭의 단위 테스트는 `tests/unit/<탭>-*.test.ts`로 이름 짓는다(예: `tests/unit/extract-settings.test.ts`). `npm run test:unit`은 `tests/unit/` 바로 아래의 `*.test.ts`만 돌리므로 하위 폴더에 두지 않는다.
- 탭 입구 파일 이름(`ExtractTab.tsx` 같은)은 바꾸지 않는다. `App.tsx`가 이 파일을 지연 로딩하고, 빌드가 이 이름으로 탭 결과 파일을 만든다. 입구 파일은 `export default`로 컴포넌트를 내보낸다.
- 탭 안에서는 `import()`(동적 가져오기)를 쓰지 않는다. 결과 파일이 늘어나 다른 탭과 부딪친다.
- 공유 부분(`app/`, `styles/`, `components/`, `copy/common.ts`, `api/http.ts`, `lib/`)은 탭 작업과 섞지 않고 **따로 PR을 낸다.** 탭에 새 공유 부품이 필요하면 그 부품을 먼저 공유 PR로 넣는다.
- 새 npm 패키지는 `package.json`과 `package-lock.json`을 바꾸고 모든 탭이 함께 쓰므로, **패키지를 더하는 일만 담은 PR**로 따로 낸다.

## 빌드 결과물

- 결과는 `src/autoetl/ui/static/`에 쓰고 저장소에 커밋한다. 파일 이름에 해시를 붙이지 않고, 같은 소스는 늘 같은 바이트로 빌드된다. CI가 다시 빌드해 커밋된 결과와 같은지 확인한다.
- 공유 코드는 `assets/index.js`와 `assets/index.css`에, 탭 폴더의 코드는 그 탭의 `assets/<이름>Tab.js`와 `assets/<이름>Tab.css`에 들어간다. 그래서 탭 폴더 안의 줄을 고치면 그 탭의 결과 파일만 바뀐다. 다만 탭이 그동안 아무도 쓰지 않던 공유 부품이나 라이브러리 기능을 처음 쓰면 `assets/index.js`도 바뀐다.
- 탭이 `react` 같은 CommonJS 패키지를 직접 가져오면 `assets/rolldown-runtime.js`가 생긴다. 여러 결과 파일이 함께 쓰는 번들러 실행 도우미(CommonJS 모듈을 ES 모듈로 바꾸는 함수 등)다. 이름은 고정이고, `index.html`이 `modulepreload`로 미리 불러온다.
- `THIRD_PARTY_LICENSES.md`는 빌드가 번들에 들어간 패키지의 라이선스를 모아 쓴 파일이다. 손대지 않는다.
- 새 main으로 리베이스한 뒤에는 `npm ci && npm run build`를 다시 실행하고 그 결과를 커밋한다. **`src/autoetl/ui/static/` 아래 파일은 충돌이 나도 손으로 합치지 않는다.** 다시 빌드한 결과가 정답이다.

## 디자인 토큰

색·글자·간격은 `src/styles/tokens.css`의 CSS 변수만 쓰고, 값을 직접 적지 않는다. 이름은 뜻으로 붙였다(`--color-text`, `--color-text-secondary`, `--color-border-input`, `--color-focus`, `--color-status-error` 등).

- 흑백에 상태 색 세 가지(오류·주의·완료)만 쓴다. 상태 색은 늘 아이콘·글자와 함께 쓰고 색만으로 뜻을 전하지 않는다.
- 글자는 바탕과 4.5:1 이상, 입력칸·버튼 테두리와 초점 표시는 3:1 이상이다. `--color-divider`(#e5e5e5)는 뜻이 없는 구분선에만 쓴다.
- 간격은 4px 단위(`--space-1`~`--space-12`), 누르는 곳은 24px 이상, 보통 버튼은 36px, 주 버튼은 40px이다. 주 버튼은 한 화면에 하나만 둔다.
- 크기는 rem으로 적어 브라우저 글자 크기를 따른다. 글꼴은 PC에 있는 것만 쓰고 웹 글꼴을 내려받지 않는다. 화면은 외부로 어떤 요청도 보내지 않는다.
- 폭 320px과 400% 확대에서 화면이 옆으로 밀리지 않아야 한다. 넓은 표만 표 상자 안에서 옆으로 민다.
- 움직임은 0.15초 이하로 짧게 쓰고, `prefers-reduced-motion`이면 `base.css`가 끈다.
- 스타일은 CSS Modules(`<이름>.module.css`)로 쓴다. 클래스 이름은 `src/` 기준 경로와 클래스 이름으로 만들어진다(예: `features-search-ResultList_root`). 그래서 두 탭에 같은 이름의 CSS 파일이 있어도 부딪치지 않는다.

## 공유 부품

`src/components/index.ts`에서 가져온다. 각 부품의 props는 파일 위쪽 주석에 설명이 있다.

| 부품 | 쓰는 곳 |
| -- | -- |
| `Button` | 모든 버튼. `variant`는 `primary`·`secondary`(기본)·`quiet`. 누를 때는 `onPress`를 쓴다. |
| `AppTabs` | 위쪽 탭 줄(App.tsx). 화살표 키로 이동하는 표준 탭이다. 한 번 연 탭은 다른 탭을 보는 동안에도 숨겨진 채 남아 있어서 읽던 일과 결과가 사라지지 않는다. 그래서 숨겨진 동안에도 그 탭의 요청과 타이머는 계속 돈다. |
| `StepIndicator` | `1 넣기 · 2 확인·다듬기 · 3 내보내기`. `current`에 지금 단계를 넘긴다. |
| `StatusBar` | 아래 진행 상태 줄. `message`, `tone`, `startedAt`, `endedAt`. 문장은 `role="status"`로 읽히고 걸린 시간은 따로 보인다. |
| `ErrorNotice` | 오류 안내. `AppError`를 그대로 넘기면 무슨 일 · 왜 · 어떻게와 자세히(원래 글)를 보여 준다. |
| `Details` | 접힌 칸. 기술 세부와 세부 설정을 넣는다. |
| `FileDropZone` | 끌어 놓기와 [파일 고르기]. 형식으로 거르지 않으니 `onFiles`에서 확장자와 크기를 확인한다. |
| `Workspace` | 목록 · 결과 · 근거 세 칸과 내보내기 줄, 진행 상태 줄. 화면 폭에 따라 두 칸·한 칸으로 바뀐다. |
| `DataTable` | 표와 [엑셀용으로 복사하기]. 탭 구분 글과 HTML 표를 함께 복사한다. |
| `MarkdownView` | 마크다운을 문서처럼 보여 준다. 원본 HTML은 글자로만 보이고 그림은 대체 글로 바뀐다. |
| `ConfirmDialog` | 확인 창. 버튼은 하는 일을 이름으로 단다(예/아니오 금지). 주 버튼에 처음 초점이 가고 Esc는 취소다. |
| `TrustBadge` | "내 PC에서만 실행 · 외부 전송 없음". |
| `ComingSoon` | 준비 중인 탭. 지금 화면(`/`)으로 가는 링크를 보여 준다. |
| `VisuallyHidden`, `Icon` | 화면 낭독기 전용 글, 장식용 아이콘. |

`App.tsx`는 끌어 놓기 칸 밖에 떨어진 파일을 막는다(그대로 두면 브라우저가 그 파일을 열어 화면과 읽은 결과가 모두 사라진다). 파일을 받을 곳은 `FileDropZone`이나 React Aria의 `useDrop`으로 만든다. 이들은 이벤트를 먼저 받아 취소하므로 막히지 않는다.

탭 화면은 `app/TabContent.tsx`가 처음 열 때 불러온다. 탭 파일을 받지 못하거나 탭이 그리다 오류를 내면 그 탭 칸에만 오류 안내와 [다시 불러오기]가 뜨고, 다른 탭과 그 결과는 그대로 남는다.

공유 도우미: `api/http.ts`의 `getJson`·`postJson`(JSON 헤더, `AbortSignal`, 실패는 모두 `AppError`), `lib/errors.ts`의 `AppError`·`errorFromResponse`·`unsupportedFileError`, `lib/files.ts`의 `readFileAsBase64`·`checkFile`·`formatBytes`, `lib/tableClipboard.ts`, `lib/markdown.ts`, `lib/korean.ts`(`withParticle('표 2', '을/를')` → "표 2를").

## 문구

- 화면의 모든 문장은 해요체로 쓴다("읽는 중이에요", "표 3개를 찾았어요"). 버튼·탭·표 머리글 같은 짧은 이름은 "ZIP으로 저장하기", "본문"처럼 쓴다.
- 사용자의 말로 쓴다. 업로드 → 넣기, 다운로드 → 저장하기, 파싱 → 읽기, 컬럼 → 열, 인덱싱 → 검색 준비. 기술 정보는 "자세히" 안에만 둔다.
- 진행 문구에는 "단계"라는 말을 쓰지 않는다(위쪽 세 단계와 헷갈린다).
- 오류는 **무슨 일 · 왜 · 어떻게** 순서로 쓰고, 다음에 할 일을 버튼으로도 준다. 사용자를 탓하는 말("잘못된", "유효하지 않은")은 쓰지 않는다. 서버가 보낸 원래 글은 바꾸지 않고 "자세히"에 그대로 둔다. 자주 나는 서버 오류는 `lib/errors.ts`가 HTTP 상태와 알려진 글 앞부분으로 골라 바꾼다.
- 여러 탭이 함께 쓰는 문구는 `copy/common.ts`에, 한 탭만 쓰는 문구는 그 탭의 `copy.ts`에 둔다. 이름 뒤에 조사를 붙일 때는 `withParticle`을 쓴다.
- 예시 값은 모두 지어낸 값을 쓴다. 실제 문서나 개인정보를 테스트에 넣지 않는다.

## 라이선스

- 화면과 함께 배포되는 런타임 패키지(`dependencies`)는 허용형 라이선스만 쓰고, MPL-2.0은 쓰지 않는다.
- MPL-2.0은 개발·검사에만 쓰고 배포물에 들어가지 않는 도구 `axe-core`, `@axe-core/playwright`, `lightningcss`(`lightningcss-*` 포함)에만 허용한다.
- 저장소 루트에서 `uv run python tools/npm_license_check.py`로 확인한다. CI도 같은 검사를 한다.
