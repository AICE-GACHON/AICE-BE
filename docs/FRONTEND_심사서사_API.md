# 프론트 연동 가이드 — 심사 서사 `GET /api/papers/{paper_id}/story`

유사 논문 목록에서 **논문을 클릭했을 때 여는 상세 패널**용 엔드포인트입니다.
"이 논문은 이런 지적을 받고 이렇게 고쳤다"를 한 번의 호출로 전부 내려줍니다.

`GET /api/papers/{id}`(상세) + `/reviews` + `/revisions`를 시간축으로 엮은 것이라
**이 셋을 따로 부를 필요가 없습니다.**

**호출 시점**: 사용자가 논문을 명시적으로 열었을 때만. 목록 렌더링 시 미리 부르지
마세요 — 첫 호출은 외부 API(OpenReview)를 타서 수 초 걸립니다. 두 번째부터는
서버 캐시라 빠릅니다. 로딩 스피너가 필요합니다.

Swagger에서 바로 눌러볼 수 있습니다: `/docs` → `GET /api/papers/{paper_id}/story`

---

## 응답 = 세 덩어리

```jsonc
{
  "paper_id": 27030, "title": "...", "venue": "ICLR 2025",
  "year": 2025, "decision": "accept-oral",

  "journey":  { ... },   // ① 재투고 궤적
  "timeline": [ ... ],   // ② 심사 시간축  ← 화면의 메인
  "narrative":{ ... },   // ③ 요약 (상단 카드)

  "timeline_supported": true,
  "caveats": ["..."],    // 사용자에게 그대로 노출할 안내 문구
  "cached_at": null      // 캐시에서 나온 응답이면 최초 생성 시각(ISO)
}
```

세 덩어리는 **서로 독립적으로 비어 있을 수 있습니다.** 외부 API가 실패해도
`journey`는 나오고, 서버 LLM이 꺼져 있어도 `timeline`은 나옵니다. 한 부분이
비었다고 화면 전체를 에러로 처리하지 마세요.

---

## ① `journey` — 재투고 궤적 (상단 배지)

```jsonc
{
  "stops": [{
    "paper_id": 27030, "openreview_id": "EzjsoomYEb", "title": "...",
    "venue": "ICLR 2025", "year": 2025, "decision": "accept-oral",
    "avg_rating": 8.0, "rating_count": 3, "rating_vs_venue": 2.85,
    "is_query": true,               // 사용자가 클릭한 그 논문
    "match_method": "title_exact",  // 직전 stop과 이어붙인 근거 (첫 stop은 null)
    "match_confidence": 0.95
  }],
  "outcome": "single",  // single | improved | still_rejected | mixed
  "message": null       // 있으면 그대로 한 줄로 노출
}
```

`stops`가 2개 이상이면 `ICLR 2024 reject → NeurIPS 2024 accept` 식으로 그려주세요.
**`outcome: "improved"`가 이 기능의 하이라이트**입니다 (떨어졌다가 고쳐서 붙은
케이스). `stops`가 1개면(`outcome: "single"`) 배지 자체를 숨겨도 됩니다.

| `outcome` | 뜻 |
|---|---|
| `single` | 재투고 기록 없음 |
| `improved` | 탈락한 뒤 다시 내서 통과 |
| `still_rejected` | 재투고했지만 또 탈락 |
| `mixed` | 그 외 |

⚠️ `avg_rating`은 **stop끼리 직접 비교하면 안 됩니다** — 학회마다 점수 척도가
다릅니다(ICLR 2020만 1~8점). 비교는 `rating_vs_venue`(같은 학회 평균 대비)로만
하세요.

⚠️ 재투고 링크는 제목 일치로 **추정**한 것이라 확실한 사실이 아닙니다.
`match_method`가 `title_author_fuzzy`면 신뢰도가 낮으니 단정적으로 표현하지 마세요.

---

## ② `timeline` — 시간순 이벤트 배열 (메인)

모든 이벤트의 공통 필드:

| 필드 | 내용 |
|---|---|
| `event_id` | 고유 키 (React key로 사용) |
| `at` | epoch ms. 정렬 키 (이미 정렬돼 있음) |
| `date` | `"2024-11-18 02:17"` (KST, 표시용) |
| `kind` | 아래 표 참고 |
| `kind_label` | 화면 표시용 한국어 라벨 ("리뷰", "저자 응답" …) |
| `actor` | `"리뷰어 8Wou"` / `"저자"` / `"AC"` / `"PC"` |
| `headline` | 접힌 상태에서 보여줄 한 줄 |

**`headline`만 쓰면 접힌 목록이 완성됩니다.** 펼치면 `kind`별 추가 필드를
보여주세요.

| `kind` | 추가 필드 | 화면 |
|---|---|---|
| `review` | `review{rating, rating_raw, confidence, summary, strengths, weaknesses, questions, is_unsplit}` | 리뷰 카드 |
| `review_update` | `rating` (최종 점수) | 한 줄 표시만 |
| `rebuttal` | `text` (저자가 쓴 응답 원문) | 저자 색상 |
| `comment` | `text` (리뷰어·AC가 쓴 코멘트) | |
| `author_revision` | `changes[]`, `is_baseline` | diff 뷰 |
| `meta_review` | `text` | AC 총평 |
| `decision` | `text` | 최종 결과 |

### 실제 응답 예시

```jsonc
{"kind": "review", "date": "2024-11-03 06:29", "actor": "리뷰어 8Wou",
 "headline": "리뷰어 8Wou — 8",
 "review": {"rating": 8.0, "rating_raw": "8", "confidence": 3.0,
            "summary": "The paper presents an in-depth exploration of …"}}

{"kind": "rebuttal", "date": "2024-11-18 02:17", "actor": "저자",
 "headline": "저자: General Response",
 "text": "We thank all the reviewers for their positive evaluations …"}

{"kind": "comment", "date": "2024-11-18 08:57", "actor": "리뷰어 8Wou",
 "headline": "리뷰어 8Wou의 코멘트",
 "text": "I thank the authors for their rebuttal. … will raise my score accordingly."}

{"kind": "review_update", "date": "2024-11-13 01:16", "actor": "리뷰어 sF9V",
 "headline": "리뷰어 sF9V가 저자 응답 이후 리뷰를 수정했습니다 (최종 8) — 수정 전 내용은 공개되지 않습니다",
 "rating": 8.0}

{"kind": "decision", "date": "2025-01-22 14:27", "actor": "PC",
 "headline": "최종 결과: Accept (Oral)"}
```

### `author_revision.changes[]`

기존 `GET /api/papers/{id}/revisions`의 `FieldChange`와 **완전히 동일한 구조**입니다.
이미 diff 뷰를 만들었다면 그대로 재사용하세요.

- `kind: "text"` → `segments[]`(`op`: `equal` / `insert` / `delete`)에 색만 입히면
  됩니다. `similarity`는 1.0이 동일.
- `kind: "file"` → `after`가 `"교체됨"` / `"추가됨"` / `"삭제됨"`,
  `before_url` · `after_url`로 그 시점 파일을 실제로 내려받을 수 있습니다.
- `kind: "value"` → `before` / `after` 단순 비교.

`is_baseline: true`인 항목은 **관측 가능한 첫 버전**이라 diff가 없습니다.
"수정했다"가 아니라 "이 시점 이전은 알 수 없다"는 뜻입니다.

### `review.is_unsplit`

2023년 이전 학회 리뷰는 강점/약점이 분리되지 않아 `weaknesses`에 **리뷰 본문
전체**가 들어옵니다. `is_unsplit: true`면 "약점"이라고 라벨을 붙이지 말고
**"리뷰 본문" 한 덩어리**로 표시하세요.

---

## ③ `narrative` — 요약 (상단 카드)

```jsonc
{
  "headline": "위상학적 딥러닝 모델의 표현력을 다룬 이 논문은 설명 난해함과 실험적 검증 부족을 지적받았고, 저자들은 런타임 비교 결과를 제시하고 추가 설명 및 실험 확장을 약속했습니다.",
  "reviewers_asked": ["Figure 5의 텐서 다이어그램에서 … 불명확함", "…"],
  "authors_changed": ["ZINC·MOLHIV 벤치마크 비교표를 답변에 제시함", "…"],
  "outcome_note": "…",
  "evidence_scope": "abstract_only",   // abstract_only | replies_only
  "used_llm": false
}
```

⚠️ **`used_llm: false`면 LLM이 꺼져 있어 기계적인 스텁 문장입니다.**
(예: `"서술 명확성 등을 지적받았고, 저자 응답 8건이 있었습니다."`)
위 예시처럼 풍부한 문장은 서버에서 LLM을 켜야 나옵니다. **디자인은 두 경우를 다
견디게 해주세요** — 스텁은 문장이 짧고 밋밋하며 `reviewers_asked`가
`["서술 명확성 3건", "이론적 엄밀성 2건"]` 같은 카운트 나열이 됩니다.

---

## 🚨 반드시 지켜야 할 것 3가지

데이터로 뒷받침되지 않는 표현이라, 만들면 **사실과 다른 화면**이 됩니다.

### 1. "6점 → 7점 상향" 같은 UI를 만들면 안 됩니다

OpenReview가 수정 전 점수를 공개하지 않아 **복원이 불가능**합니다(리뷰 edit 이력의
첫 항목이 빈 채로 내려옴 — 표본 3건 전부). `review_update` 이벤트는 "이 시점에
리뷰가 수정됐고 최종 점수는 8점"까지만 말합니다. **화살표·증감 뱃지 금지.**

### 2. "실험을 추가했다" 같은 표현 금지

확인 가능한 수정은 **제목·초록·첨부파일까지**입니다. 논문 본문은 PDF 안이라 읽을
수 없습니다. `evidence_scope`가 그 범위를 알려줍니다.

- `abstract_only` — 제목·초록 diff까지 확인함
- `replies_only` — 수정 이력 비공개 학회라 리뷰·응답만 확인함

### 3. `timeline_supported: false`는 "심사가 없었다"가 아닙니다

**"공개되지 않는다"** 는 뜻입니다. 빈 목록으로 처리하지 말고 **`caveats` 문구를
그대로 노출**하세요. `caveats`는 이미 사용자에게 보여줄 수 있는 완성된 한국어
문장이라 가공 없이 그대로 쓰면 됩니다.

---

## 데이터가 없는 경우도 흔합니다

- **저자 수정 diff는 소수에서만 옵니다.** 무작위 14편 중 3편. ICLR 2024·NeurIPS는
  대개 게재 확정본만 열립니다 → `author_revision`이 `is_baseline: true` 하나뿐이거나
  아예 없는 게 **정상**입니다. 이때 "수정 없음"이라고 쓰면 안 되고, `caveats`가
  대신 설명합니다.
- **2023년 이전 학회**는 diff가 없지만 **리뷰·저자 응답 타임라인은 나옵니다**
  (ICLR 2022 실측: 리뷰 4건 + 저자 응답 8건).
- 심사 전 철회·데스크리젝된 논문은 `timeline`이 비어 있을 수 있습니다
  (코퍼스 43,515편 중 481편이 리뷰 자체가 없음).

---

## 확인용 paper_id

| paper_id | 상태 |
|---|---|
| `27030` | ICLR 2025 accept-oral — 이벤트 21개, 저자 수정 diff 있음 |
| `11745` | ICLR 2024 accept-poster — 이벤트 23개, 리뷰 수정 3건 |
| `174` | ICLR 2023 → 2024 → 2025 3연속 reject (journey 확인용) |

---

## 관련 문서

- [DEVELOPMENT.md](DEVELOPMENT.md) §7 — 전체 엔드포인트 목록
- [DEVELOPMENT.md](DEVELOPMENT.md) §8 — AI 파트 공개 API와 이 응답의 근거 한계
- [DEVELOPMENT.md](DEVELOPMENT.md) §6 — `Report`(분석 결과)를 화면에 옮길 때의 주의사항
