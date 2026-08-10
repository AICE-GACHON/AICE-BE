# 리비전 본문(PDF) Diff 기능 — 구조·처리 로직·알려진 오류 유형

작성 2026-08-10

---

## 요약

논문이 리뷰를 거치며 리비전을 여러 번 낼 때, 그 사이 **본문 PDF 자체가 어떻게
바뀌었는지**(문장, 그림, 표, 알고리즘, 수식)를 리비전 쌍마다 diff로 보여주는
기능이다. **LLM을 전혀 안 쓴다** — PyMuPDF로 PDF를 파싱하고 `difflib`로 텍스트를
비교하는, 100% 결정론적인 파이프라인이다. 그래서 비용은 0원이고, 같은 입력이면
항상 같은 출력이 나온다(재현 가능 — 디버깅에 유리하다).

핵심 난이도는 diff 로직이 아니라 **PDF에서 "그림 몇 번이 어디부터 어디까지인지"를
정확히 잘라내는 것**에 있다. LaTeX가 그림·표·알고리즘·수식을 배치하는 방식이
논문마다, 심지어 같은 논문의 페이지마다 미묘하게 달라서, 좌표 기반 휴리스틱이
계속 깨진다 — 이 문서 뒷부분(6절)이 실제로 겪은 오류들이다.

---

## 1. 무엇을 하는 기능인가

`GET /api/papers/{paper_id}/revisions/body-diff`

- 논문의 리비전 이력(`/revisions`이 이미 하는 title/abstract/keywords 비교)에
  **본문·그림·표 diff를 추가로 얹은** 버전.
- `refresh=true`면 캐시 무시하고 재계산 — 로그인 필요(비로그인 스캔 방지).
- Rate limit `10/hour`(IP 기준) — `/revisions`(`30/hour`)보다 좁다. 캐시 없는
  호출 1번이 PDF 여러 개를 다운로드+파싱+diff하는 무거운 작업이라서다.
- 프론트는 현재 `AICE-FE/src/dev/BodyDiffTest.jsx`라는 **개발자용 테스트
  페이지**에만 붙어 있고, 실제 서비스 화면(`ResultReport.jsx`)에는 아직 안
  붙었다.

---

## 2. 전체 흐름

```
GET /revisions/body-diff
        │
        ▼
get_paper_revisions_with_body(paper_id)          [revisions.py]
        │
        ├─ paper_body_diffs 테이블에 캐시 있고 30일 안 지났으면 → 바로 반환
        │
        └─ 캐시 미스 →
                │
                ▼
        get_paper_revisions(paper_id)             [기존 함수, title/abstract/keywords diff]
                │
                ▼
        attach_body_diffs(revisions)              [revisions.py, 이 기능의 핵심]
                │
                ├─ 리비전마다 pdf FieldChange(before_url/after_url) 찾기
                ├─ 각 URL의 PDF를 OpenReview에서 다운로드 (중복 URL은 캐시)
                ├─ extract_full_text(pdf) → 본문 텍스트          [extract.py]
                ├─ extract_media_images(pdf) → {"Figure 1": png, ...}
                ├─ extract_media_captions(pdf) → {"Figure 1": "캡션 문구", ...}
                ├─ extract_box_texts(pdf) → {"Box <해시>": 텍스트}
                │
                ├─ _word_diff_multi_paragraph(before_text, after_text)
                │       → field="body" FieldChange (kind="text", segments=[...])
                │
                └─ 그림/표/알고리즘 라벨마다
                        → field="<label>" FieldChange (kind="image", before_image/after_image)
                │
                ▼
        paper_body_diffs 테이블에 저장 (JSONB)
```

`attach_body_diffs`는 **순수 함수가 아니라 PDF 다운로드(네트워크 I/O)를
포함**한다. 그래서 `build_revisions`(제목/초록/키워드 diff, `/story`와 공유하는
순수 함수)와는 완전히 분리돼 있다 — `/story`가 이 기능 때문에 느려지거나 비용이
붙는 일은 없다.

---

## 3. PDF 추출 로직 (`paper_assistant/pdf/extract.py`)

### 3.1 텍스트 추출 — `extract_full_text`

페이지를 통짜로 `get_text("text")` 하지 않는다. 문단 경계 정보가 사라지기
때문이다. 대신:

1. `_ordered_blocks(page)` — `"blocks"` 모드로 블록 경계(bbox)만 얻고,
   내용은 `get_text("text", clip=bbox)`로 다시 뽑는다. `"blocks"` 모드 자체
   텍스트는 수식이 많으면 줄 순서가 실제로 깨지기 때문이다(문장이 수식 한복판에
   끼어드는 식). 세로로 겹치는 블록(수식이 첨자 때문에 여러 블록으로 쪼개진 것)은
   먼저 하나로 합친 뒤 재추출한다.
2. 페이지마다 그림(`_figure_regions`)·표/알고리즘(`_table_regions`)·박스
   (`_box_regions`)·수식(`_equation_regions`) 영역을 찾아 **그 영역 안 텍스트는
   전부 지우고 캡션만 자리표시자로 남긴다** — `(그림 3)`, `(표 1)`, `(알고리즘 2)`,
   `(수식 5)`.
3. 영역을 못 찾은 경우(드묾) 자리표시자 대신
   `"(표 5 — 이미지 추출실패, 텍스트로 대체)\n\n{원본 캡션}"`처럼 실패 사실을
   텍스트에 남긴다 — 조용히 사라지면 원인을 알 방법이 없어서다.
4. 페이지·컬럼 경계에서 끊긴 문장은 `_stitch_split_sentences`가 다시 잇는다
   (문장부호로 안 끝나고 다음 문단이 소문자로 시작하면 원래 한 문장으로 본다).
   단 그림/표 자리표시자가 사이에 끼면 안 붙인다 — 앞뒤 문맥에 따라 같은
   자리표시자가 다른 문자열이 돼버려 diff가 오작동한다.

### 3.2 영역 판단 — 이게 진짜 어려운 부분

| 종류 | 함수 | 판단 근거 |
|---|---|---|
| 그림 | `_figure_regions` | "Figure N" 캡션 바로 앞의 마지막 진짜 문단(20단어 초과) 끝 ~ 캡션 끝. 실제 이미지/드로잉 좌표(`_figure_image_top`)로 상단을 보정 |
| 표 | `_table_regions` | 1순위 `find_tables()`가 찾은 실제 grid bbox. 못 찾으면 캡션 위/아래 괘선(가로선) 2개 이상을 찾아 그 사이를 영역으로 |
| 알고리즘 | `_table_regions`(표와 같은 함수 공유) | grid가 없으므로 항상 괘선 기반. 캡션은 **항상 내용보다 위**(표처럼 위/아래 다 가능하지 않음) |
| 박스(코드/프롬프트 인용) | `_box_regions` | 캡션이 아예 없다 — 배경색이 채워진 사각형(`get_drawings()`)의 bbox |
| 수식 | `_equation_regions` | 오른쪽 여백의 `(N)` 번호로 줄 단위 스캔 |

### 3.3 캡션·placeholder

`FieldChange.label`이 항상 "수정 전 번호" 기준이고, 저자가 번호를 재배치하면
(`v2`의 Figure 6이 `v3`에서 Figure 5가 되는 식) `after_label`에 수정 후 번호가
따로 붙는다. 프론트는 `label`/`after_label` 두 값을 각자의 문서 안에서 찾아야
한다 — 하나로 합치면 번호가 우연히 겹칠 때 서로 다른 그림이 뒤섞인다.

이미지는 **픽셀 비교로 "바뀌었는지" 판단하지 않는다.** `similarity`는
`kind="text"`(본문 문장)에만 있고 `kind="image"`에는 없다 — 그림·표·수식은 항상
전/후 이미지를 나란히 보여주고 **사람이 보고 판단**하는 게 설계 의도다(이전에
이미지 유사도 자동 판정을 시도했다가 설계 원칙과 안 맞아 되돌린 적 있다 — 6절
참고).

---

## 4. Diff 로직 (`paper_assistant/query/revisions.py`)

- **`_word_diff_multi_paragraph`**: `difflib.SequenceMatcher` 기반 단어 단위
  diff. `MAX_BODY_SEGMENTS = 2000`으로 세그먼트 수를 제한한다(문서 길이가 아니라
  "수정 지점 개수"에 비례하므로, 완전히 다른 문서가 잘못 매칭되는 병리적
  케이스만 자른다).
- **`_match_moved_paragraphs`**: 삭제된 문단과 삽입된 문단의 유사도가
  `_MOVE_MATCH_THRESHOLD = 0.6` 이상이고 `_MOVE_MIN_WORDS = 6` 단어 이상이면
  "이동(moved)"으로 재분류한다 — 순수 삭제+삽입 대신 "위치만 옮겨졌다"고
  보여준다. 순수 placeholder(`(그림 3)` 같은)는 이동 판정에서 제외된다
  (`_MEDIA_PLACEHOLDER`).
- **`_rematch_media_labels`**: 같은 그림이 리비전 사이에 번호가 바뀐 경우
  (`_media_similarity`, 이미지 signature 비교)를 감지해 `after_label`을 채운다.
- 캐시: `paper_body_diffs` 테이블, `STALE_AFTER = 30일`. `story.py`의 캐시
  패턴을 그대로 따른다.

---

## 5. 성능·비용

- **LLM 사용 없음** — `extract_full_text`/`attach_body_diffs` 어디에도 LLM
  호출이 없다(`extract_title_abstract`와는 완전히 다른 코드 경로).
- `MAX_BODY_PAGES = 60` — 이보다 긴 PDF는 **양쪽을 잘라서 비교하지 않고 그
  transition 자체를 건너뛴다**(양쪽을 독립적으로 절삭하면 실제 수정이 잘린
  부분 너머에 있을 때 유사도가 왜곡될 수 있어서).
- 캐시 히트면 즉시 반환, 캐시 미스면 PDF 여러 개 다운로드+파싱이라 수 초 걸릴
  수 있다.

---

## 6. 알려진 오류 유형 (실측)

이번 세션(2026-08-09~10)에 28599/26079/23091 세 논문을 실제 PDF와 대조하며
찾아 고친 것과, 고치지 않고 남겨둔 것.

| # | 증상 | 원인 | 상태 |
|---|---|---|---|
| 1 | 알고리즘 의사코드(`1: Input:...`)가 통째로 일반 텍스트로 새어 나와 앞뒤 문단과 뒤섞임. 28599의 "삭제/삽입 폭주" 노이즈 대부분이 이것 | `_table_regions`의 괘선 선택 로직이 "캡션과 더 가까운 쪽"을 고르는데, 알고리즘 캡션 바로 위엔 항상 자기 테두리 괘선이 있어(간격 1~2pt) 이게 매번 이겨서 영역이 캡션 한 줄로 쪼그라듦 | ✅ 고침 — 알고리즘 캡션은 위/아래 비교 없이 항상 아래(내용)만 보게 함 |
| 2 | 페이지마다 같은 y좌표에 찍히는 학회 템플릿 장식선이 표 괘선으로 오인됨 | `_horizontal_rules`가 장식선과 진짜 표 괘선을 구분 못함 | ✅ 고침 — `_repeating_rule_ys`로 페이지의 30% 이상에서 반복되는 y좌표를 제외 |
| 3 | 알고리즘 두 개가 페이지에 좌우로 나란히 있으면 괘선이 중복 카운트돼 영역이 또 쪼그라듦 | 같은 y좌표의 괘선이 좌/우 도형 2개로 잡힘 | ✅ 고침 — 괘선 y좌표 반올림 후 중복 제거 |
| 4 | 두 알고리즘이 한 페이지에 세로로 붙어 있으면 앞쪽이 뒤쪽의 테두리 괘선을 자기 종료선으로 훔쳐감 | "다음 캡션 직전까지" 탐색 범위가 다음 알고리즘 자신의 테두리까지 먹음 | ✅ 고침 — 알고리즘은 `below[-1]`(마지막 괘선) 대신 `below[1]`(자기 종료선)만 사용 |
| 5 | 수식이 많은 문단에서 `"the following paper"`가 `"h f ll i H..."`처럼 글자가 토막나 끼어듦 | `page.get_text("text", clip=rect)`가 clip 경계에 걸친 줄을 부분 포함시켜 다음(병합 안 된) 블록의 글자 일부만 새어 들어옴 | ✅ 고침 — `_strip_clip_boundary_leak`: 끝에 짧은(1~3자) 알파벳 토큰이 5개 이상 연속이면 `page.get_textbox(rect)`로 대조해 실존 안 하면 잘라냄 |
| 6 | 표 헤더 행(`Dataset Avg. A-c A-e HS LA PQ WG`)이 표 자리표시자 밖으로 새어 나옴 | find_tables()가 헤더 행까지는 grid로 안 잡고, 헤더-그리드 간격이 `_HEADER_GAP_LIMIT`보다 커서 상단 확장 로직이 못 잡음 | ❌ 안 고침 — 병합 안 된 단일 블록이라 5번 수정과 무관. 양쪽 버전에 똑같이 있어 diff 노이즈는 안 만듦(항상 `equal`) |
| 7 | (조사했으나 기능 자체를 안 만들기로 함) 라벨 없는 수식을 자동으로 크롭하는 기능 | 인라인 수식 조각이 단독 수식으로 오탐되는 경우가 많고, 안전한 일반 규칙을 못 찾음 | ❌ 전체 되돌림 — 라벨 있는 수식만 크롭, 나머지는 텍스트로 유지 |
| 8 | (조사했으나 기능 자체를 안 만들기로 함) 같은 라벨 그림/표의 내용이 실제로 바뀌었는지 유사도로 자동 판정 | 설계 의도 자체가 "전/후 나란히 보여주고 사람이 판단"이라 시스템 판정은 범위 밖 | ❌ 되돌림 — `FieldChange.similarity`는 image kind에 안 씀 |

### 원칙적으로 남는 리스크

5번 수정은 **검증(대조)만 하고 채택은 안 하는** 방식이라 안전하지만, 이론적으로
"진짜 문장이 짧은 대문자 약어 5개 이상으로 끝나는" 극단적 케이스가 있다면
`get_textbox()`에도 똑같이 나타나야 트림이 안 걸린다 — 3개 논문·5개 리비전
전체를 스캔해서 오탐 0건을 확인했지만, 새 논문에서 이 패턴이 다시 나타날
가능성 자체를 0으로 만든 것은 아니다.

---

## 7. 디버깅 방법론 — 다음에 이상한 diff를 발견하면

**절대 "그럴듯해 보이는" 수정을 코드만 보고 하지 않는다.** 이번 세션의 모든
수정은 아래 순서로 확인했다:

1. `docker exec paper-assistant-db psql -U paper -d paper_assistant -c "DELETE FROM paper_body_diffs WHERE paper_id = ...;"` — 캐시 지우기.
2. 백엔드 재시작(`--reload` 없이 뜨므로 코드 수정 후 **반드시 재시작** — 이번
   세션에 재시작을 깜빡해서 "고쳤는데도 안 고쳐진 것처럼 보인" 해프닝이 있었다).
3. `curl .../revisions/body-diff`로 실제 응답을 받아 `non-equal` 세그먼트만
   추려서 눈으로 읽는다.
4. 의심스러운 부분은 **OpenReview 첨부 URL에서 실제 PDF를 다운로드해
   PyMuPDF로 직접 열어** 원문과 대조한다(`before_url`/`after_url`은
   `field=="pdf"`인 FieldChange에 있다).
5. `page.get_text("blocks")`, `get_drawings()`, `_table_regions()` 등을 직접
   호출해 좌표를 찍어보고 가설을 세운다.
6. 고친 뒤에는 **pytest + ruff + 실제 PDF로 만든 회귀 스크립트**(이미지 개수·
   중복 placeholder·삭제/삽입 근접 중복·전체 텍스트 재구성 일치 체크) 전부
   통과해야 한다. 스크래치패드에 `debug_26079.py`/`debug_28599.py` 형태로
   이미 있다.
7. 수정 하나가 다른 논문·다른 페이지에 영향 없는지 **항상 전체 재검사**한다 —
   이번 세션에 4개의 서로 다른 해결책을 시도해서 3개는 다른 곳에서 데이터
   유실이나 순서 뒤섞임이 생겨 되돌렸다(6절의 5번 항목 참고). "고쳤다"는
   느낌만으로 끝내면 안 된다.

**일반화 원칙**: 특정 논문 하나에 맞춘 하드코딩은 항상 금지. 안전한 일반
규칙을 못 찾겠으면 기능을 추가하기보다 되돌리는 쪽을 택한다(6절의 7·8번
항목이 그 예).
