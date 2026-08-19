# 🧵 myThreads — Threads '저장됨' 자동 분류 뷰어

Threads에서 저장(북마크)해 둔 글을 자동으로 가져와, **AI가 카테고리 체계를 스스로 만들고**
새 글이 저장될 때마다 그 체계에 맞춰 분류해 주는 개인용 도구입니다. Docker로 띄워서 씁니다.

* **분류** — 저장한 글 전체를 읽고 나에게 맞는 카테고리 6~12개를 자동 설계
* **내용** — 본문 + *작성자가 댓글에 이어서 쓴 글*까지 하나로 묶어 저장.
  목록 응답의 `thread_items` 묶음에 이어쓴 글이 함께 오므로 **글마다 상세 페이지를 열지 않음**
  (반대로, 내가 저장한 것이 *남의 글에 달린 댓글*이면 맥락이 없어 수집에서 제외 — `SKIP_REPLIES=0` 으로 끌 수 있음)
* **내용이미지** — 본문 이미지(캐러셀 포함)를 로컬에 내려받아 함께 표시
* **보기 전환** — 카테고리마다 카드 / 표 / 보드 중 편한 방식을 기억
* **정리** — 필요 없는 글은 **내 로컬 사본만** 삭제(본문·분류·이미지 파일).
  Threads 계정의 '저장됨' 목록은 건드리지 않으며, 지운 글은 다음 동기화에서 다시 들어오지 않음
* **자동 갱신** — 하루 2회(기본 09:10, 21:10 KST) 수집 → 새 글만 분류
* **증분 수집** — 저장됨 목록은 최근 저장이 위이므로, **이미 가진 글을 만나는 즉시** 훑기를 멈춤 (코드 또는 작성자+작성시각으로 대조).
  평소 동기화는 수십 초. 일주일에 한 번(그리고 중간에 멈춘 다음 실행에는) 자동으로 끝까지 훑어 누락 확인

---

## 왜 스크래핑인가

Threads 공식 API에는 '저장됨(북마크)' 목록을 읽는 엔드포인트가 없습니다.
그래서 **내 로그인 세션으로 브라우저를 띄워 내 저장 목록을 읽는** 방식을 씁니다.
계정 보호를 위해 기본 수집 주기는 하루 2회로 잡아두었습니다(사람이 앱을 두 번 여는 정도).
너무 자주 돌리면 계정 제한을 받을 수 있으니 `SYNC_TIMES`를 과하게 늘리지 마세요.

---

## 설치 (5분)

```bash
cd myThreads
cp .env.example .env         # GEMINI_API_KEY 입력 (https://aistudio.google.com/apikey)
make build                   # 이미지 빌드 (첫 빌드 3~5분)
```

> **API 키는 '분류'에만 쓰입니다.** 글을 읽어와 저장하는 수집 단계는 내 브라우저 세션만
> 사용하며 LLM을 전혀 호출하지 않습니다. 저장된 본문·이어쓴 댓글 텍스트를 Gemini에 보내
> 카테고리·한 줄 요약·태그를 받아오는 데에만 사용합니다. 이미지는 전송하지 않습니다.

### 1) 최초 1회 Threads 로그인

```bash
make login
```

터미널에 안내가 뜨면 브라우저로 접속:

```
http://localhost:6080/vnc.html?autoconnect=1&resize=scale
```

컨테이너 안의 크롬 화면이 그대로 보입니다. **Instagram 계정으로 로그인**하면
세션이 `data/state.json`에 저장되고 컨테이너가 스스로 종료됩니다.
(2단계 인증도 이 화면에서 그대로 하면 됩니다.)

> noVNC가 잘 안 열리면 대안: 브라우저 확장(Cookie-Editor 등)으로 threads.com 쿠키를
> JSON으로 내보내 `data/cookies.json`에 두고
> `docker compose run --rm worker python -m app.collector.login --from-cookies /data/cookies.json`

### 2) 실행

```bash
make up                      # http://localhost:8080  (.env 의 WEB_PORT 를 바꾸면 그 포트로)
make sync                    # 첫 수집 + 분류 (글 수에 따라 5~20분)
make logs                    # 진행 상황
```

첫 수집이 끝나면 AI가 카테고리 체계를 설계하고 전 글을 분류합니다.
이후에는 하루 2회 자동으로 새로 저장한 글만 수집·분류합니다.

---

## 화면 사용법

| 기능 | 위치 |
|---|---|
| 카테고리 이동 | 왼쪽 사이드바 |
| 정렬 | 오른쪽 위 드롭다운 — 최신순(작성일) · 최근 저장순 · 오래된순 · 작성자순 |
| 카드 / 표 / 보드 전환 | 오른쪽 위 토글 — **선택하면 그 카테고리의 기본 보기로 저장됨** |
| 검색 | 본문·요약·태그·작성자 통합 검색 |
| 태그 필터 | 사이드바 태그 클릭 |
| 상세 보기 | 카드/행 클릭 → 본문 + 이어쓴 글 + 이미지 전체 + 원문 링크 |
| 분류 수정 | 상세 창의 드롭다운 — 수동 지정한 글은 이후 자동 재분류에서 제외 |
| 로컬에서 삭제 | 상세 창 맨 아래 — **내 컴퓨터의 사본만** 지움(본문·분류·내려받은 이미지). Threads 계정의 '저장됨' 목록은 그대로이고, 다음 동기화에서 다시 가져오지 않음 |
| 지금 동기화 | 사이드바 아래 버튼 — 새로 저장한 글만 빠르게 확인 |
| 전체 다시 훑기 | 저장됨 목록을 처음부터 끝까지 확인 (누락이 의심될 때) |
| 분류 체계 재구성 | 글이 많이 쌓여 카테고리를 다시 짜고 싶을 때 |

---

## 명령어

```bash
make up / down / logs        # 실행 / 중지 / 로그
make sync                    # 수집 + 신규 글 분류 (증분 — 새 글만)
make sync-full               # 저장됨 목록을 끝까지 훑어서 수집
make classify                # 미분류 글만 분류
make reclassify              # 카테고리 체계 재설계 + 전체 재분류
make login                   # 로그인 세션 재발급 (만료 시)
make reset                   # DB 초기화 (미디어·세션 유지)
make repair                  # 본문이 잘못 들어온 글 찾기 (확인만)
make repair-apply            # 위 글들을 '다시 받기' 대상으로 표시 → make sync-full
make deleted                 # 로컬에서 지운 글 목록 확인
docker compose exec worker python -m app.pipeline taxonomy   # 현재 카테고리 확인
docker compose exec worker python -m app.pipeline delete <글id> [글id…]   # 로컬에서 삭제
docker compose exec worker python -m app.pipeline deleted --restore <글id|all>  # 삭제 취소
```

> **삭제는 로컬에만 적용됩니다.** 이 도구는 Threads에 아무것도 쓰지 않으므로
> 계정의 '저장됨' 목록은 그대로 남습니다. 지운 글은 다시 수집하지 않도록 id만 기억해 두는데,
> 되돌리고 싶으면 `deleted --restore` 로 그 기억을 지운 뒤 `make sync-full` 하면 다시 들어옵니다.

---

## 구조

```
app/
  collector/    Playwright 수집기 — 저장됨 목록 스크롤, 글 상세, 이미지 다운로드
    parser.py     Threads 내부 JSON에서 글·이미지·이어쓴 댓글 추출 (테스트 있음)
    scraper.py    브라우저 제어 + DOM 폴백
    login.py      최초 로그인 / 쿠키 임포트
  classifier/   Gemini 분류 엔진 (LLM_PROVIDER=anthropic 로 교체 가능)
    llm.py        모델 자동 해석 · JSON 응답 파싱 · 재시도
    taxonomy.py   카테고리 체계 자동 설계
    classify.py   글별 카테고리 배정 + 한 줄 요약 + 태그
  db.py         SQLite 스키마 · 저장/삭제 헬퍼 (지운 글 기억 포함)
  main.py       FastAPI 웹 UI/API
  worker.py     스케줄러 + 작업 큐
  pipeline.py   수집→저장→분류 파이프라인 (CLI)
data/           SQLite DB · 로그인 세션 · 내려받은 이미지  ← 백업은 이 폴더만
```

수집은 DOM 파싱이 아니라 **Threads 웹이 주고받는 내부 JSON을 읽는 방식**이 1순위라
화면 개편에 비교적 강합니다. JSON은 두 곳에서 들어옵니다:

1. **최초 HTML 문서에 심긴** `<script type="application/json">` — 첫 화면(목록 상단·글 상세)
2. 스크롤하며 오는 **네트워크 응답(XHR)** — 목록의 그 아래쪽

1번을 빼먹으면 목록 맨 위 글들과 글 상세를 통째로 놓쳐 DOM 텍스트로 때우게 되고,
그러면 본문에 `작성자명 23시간 …` 같은 UI 문구가 섞이고 작성시각을 잃습니다.
그렇게 오염된 글은 `make repair` 로 찾아 다시 받을 수 있습니다.
파싱이 깨지면 `app/collector/parser.py`의 `looks_like_post()` 조건만 손보면 됩니다.

## 테스트

```bash
python tests/test_parser.py      # 파서 단위 테스트 (네트워크 불필요)
python tests/test_delete.py      # 로컬 삭제 · 복원 (네트워크 불필요)
python tests/test_html_json.py   # HTML에 심긴 JSON 수집 (네트워크 불필요)
# 나머지 tests/*.py 는 가짜 Threads 서버를 띄워 실제 브라우저로 검증합니다
DATA_DIR=./data python tests/seed_demo.py   # UI 확인용 데모 데이터
```

## 문제 해결

| 증상 | 조치 |
|---|---|
| 코드를 고쳤는데 화면이 그대로 | 앱 코드는 이미지 안에 들어 있습니다. `make build && make up` 으로 다시 빌드해야 반영됩니다 (`make doctor` 1번 항목으로 확인) |
| 화면이 안 열림 | 주소는 `.env` 의 `WEB_PORT` 기준입니다. `docker ps` 로 실제 매핑 확인 |
| 지운 글이 다시 들어옴 | 삭제할 때 '다시 안 가져오기'가 꺼졌을 때 생깁니다(`?forget=false`). `make deleted` 로 기억 상태 확인 |
| 지운 글을 되살리고 싶음 | `... pipeline deleted --restore <글id\|all>` 후 `make sync-full` — 본문은 Threads에서 다시 받아옵니다 |
| 진행이 멈춤 | 10분간 진행이 없으면 worker가 스스로 재시작해 복구합니다(`WATCHDOG_MIN`). 저장된 글은 남고 다음 실행에서 이어받습니다 |
| 동기화가 '실행 중'에서 안 끝남 | `make logs` 로 마지막 줄 확인 → `docker compose restart worker` (재시작 시 죽은 작업 자동 정리). 수동 정리는 `make unstick` |
| 동기화 직후 또 일감이 생김 | `make doctor` — 상세 페이지에서 데이터를 못 건진 글이 많으면 `FETCH_DETAIL=0` 으로 두세요 |
| 동기화가 느림 | `make status` 로 전체 훑기인지 확인. 로그의 `목록 훑기 N초` / `상세 단계 N초 — 페이지를 연 글 N건` 으로 어느 쪽이 느린지 알 수 있습니다 |
| 수집 도중 끊김 | 글은 한 건씩 즉시 저장되므로 잃지 않습니다. 다시 `동기화` 하면 남은 것부터 이어받습니다 |
| 저장 글이 수백 건 | 1회 실행 상한(`RUN_BUDGET_MIN`, 기본 120분)에 걸리면 거기까지 저장하고 정상 종료합니다. 다음 실행에서 계속됩니다 |
| `로그인 세션 없음` | `make login` 다시 실행 |
| 수집 0건 | Threads UI 변경 가능성 — `make logs`에서 "저장됨 목록: JSON n개 / DOM 링크 m개" 확인 |
| 본문에 `작성자명 23시간 …` 이 섞임 | HTML에 심긴 JSON을 못 읽어 DOM 텍스트로 때운 글입니다. `make repair` → `make repair-apply` → `make sync-full` |
| 최신 글인데 목록 맨 뒤에 있음 | 작성시각을 못 얻어 `posted_at=0` 인 경우입니다. 위와 같은 방법으로 복구 |
| 분류 실패 | `.env`의 `GEMINI_API_KEY` 확인. 모델명이 없으면 사용 가능한 최신 flash 모델로 자동 대체됩니다 |
| 분류가 너무 느림/비쌈 | `GEMINI_MODEL` 을 flash-lite 계열로. 반대로 품질을 올리려면 `GEMINI_THINKING=high` |
| 이미지가 안 보임 | fbcdn 링크는 만료됩니다. `DOWNLOAD_MEDIA=1`(기본)이면 로컬 사본을 씁니다 |
