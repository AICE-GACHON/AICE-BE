"""Claude API 래퍼 (예산 제약 인지).

- 태깅/추출·심사 서사: Haiku 4.5 (저가)
- 재정렬·종합 리포트: Sonnet 5 (품질)

**모델을 가르는 기준은 "LLM이 판정자인가"다.** 재정렬은 논문 원문과 후보 50편을
읽고 무엇이 정말 비슷한지를 LLM이 결정하는 자리라 Sonnet이어야 한다. 반면 서사
요약은 코드가 이미 골라 놓은 사실(지적·응답·수정 diff)을 한국어 문장으로 옮기는
일이므로 Haiku로 충분하다 — 종합의 effort를 낮춘 것과 같은 판단이다.

예산이 빠듯해서 LLM 호출은 명시적으로 켤 때만 실행된다.
`get_llm(enabled=False)`(기본)는 None을 반환하고, 노드는 이때 결정론적
스텁 출력을 생성한다 — DAG 배선을 $0으로 검증하기 위함.
"""
import hashlib
import json
import logging

from paper_assistant import config

log = logging.getLogger(__name__)


HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5"

# Sonnet 5는 thinking을 생략하면 **adaptive thinking이 켜진 채로** 돈다
# (Sonnet 4.6은 꺼짐이 기본이었다). 그리고 max_tokens는 thinking과 본문의
# **합계** 상한이다 — 예전 값(1200)을 그대로 두면 thinking이 예산을 먹고
# 요약이 두 줄에서 잘린다(실측). 종합은 250단어짜리라 본문은 넉넉하고,
# 나머지는 thinking 몫이다.
SONNET_MAX_TOKENS = 4000
# 통계 검증은 이미 코드에서 끝났고 LLM은 사실을 문장으로 옮기는 역할이라
# 최고 수준의 추론이 필요 없다. 기본값(high)보다 한 단계 낮춰 비용을 줄인다.
SONNET_EFFORT = "medium"
# Haiku 4.5는 구세대라 effort/adaptive thinking을 받지 않는다(400). 태깅은
# 짧고 단순해서 필요도 없다 — 아무것도 넘기지 않는다.

# --- 심사 서사 (query/narrative.py) --------------------------------------------
# thinking이 없는 모델이라 이 값은 **본문만**의 상한이다 (Sonnet 계열 상수와 의미가
# 다르니 그쪽 값을 빌려 쓰지 말 것). 출력은 4필드 JSON — 한 줄 요약, 지적 2~5건,
# 대응 2~5건, 결과 한두 문장 — 이라 실제로는 700토큰 안쪽이고, 나머지는 여유다.
NARRATIVE_MAX_TOKENS = 1500

# --- 재정렬은 종합과 반대 성격이라 값이 다르다 ---------------------------------
# 종합에서 LLM은 "코드가 검증한 사실을 문장으로 옮기는" 역할이라 effort를 낮췄다.
# 재정렬에서는 LLM이 **판정자 본인**이다 — 후보 50편과 논문 원문을 읽고 무엇이
# 정말 비슷한지를 결정하는 일이고, 이 파이프라인에서 임베딩이 못 하는 바로 그
# 일이다. 여기서 아끼면 개편의 근거 자체가 없어진다.
RERANK_EFFORT = "high"
# thinking과 본문의 **합계** 상한이다. 후보 50편을 견주는 추론이라 thinking이
# 길고, 본문도 5편분 선정 이유가 들어간다. 잘리면 조용히 JSON이 깨진다.
RERANK_MAX_TOKENS = 8000


def _shape(text: str) -> str:
    """응답을 **내용 없이** 설명한다. 파싱 실패 로그에 원문 대신 쓴다.

    이 자리의 text는 논문 요약 — 사용자가 올린 미출간 원고의 내용이다. 예전에는
    앞 120~200자를 그대로 로그에 남겼는데, 로그가 외부 수집기(Sentry·CloudWatch
    등)로 흘러가면 그 자체가 유출 경로가 된다. 개인정보처리방침이 "운영 기록에
    논문 본문이 남지 않는다"고 약속하므로(app/legal/privacy.md) 여기서 지켜야 한다.

    그렇다고 아무것도 안 남기면 파싱 실패를 고칠 수 없다. 원문 없이도 원인을
    가르는 세 가지만 남긴다:
      - len  : 0이면 빈 응답, max_tokens 근처면 잘림
      - kind : 코드펜스인지 산문인지 — 프롬프트 문제인지 모델 문제인지 갈린다
      - sha  : 같은 실패가 반복되는지 대조용 지문 (원문 복원은 불가능)
    """
    if not text:
        return "empty"
    head = text.lstrip()[:1]
    kind = {"{": "object", "[": "array", "`": "fence"}.get(head, "other")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"len={len(text)} kind={kind} sha={digest}"


class ClaudeLLM:
    def __init__(self):
        import anthropic   # 지연 import — LLM 끌 땐 설치/키 불필요
        # API 키는 anthropic SDK가 ANTHROPIC_API_KEY 환경변수에서 직접 읽는다.
        self.client = anthropic.Anthropic()

    def text(self, model: str, system: str, user: str, max_tokens: int = 2048,
             **params) -> str:
        """모델 호출 후 텍스트 블록을 이어붙여 반환한다.

        **모든 text 블록을 합친다.** 예전에는 첫 블록만 꺼냈는데, 응답이 여러
        블록으로 쪼개져 오면 뒷부분이 통째로 사라진다.

        params로 thinking/output_config 등을 넘긴다 — 모델마다 받는 값이 달라서
        (Haiku 4.5는 effort를 거부한다) 여기서 일괄 적용하지 않는다.
        """
        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            **params,
        )
        self._log_usage(model, resp)
        if resp.stop_reason == "max_tokens":
            # 조용히 잘린 응답은 알아채기 어렵다. thinking이 켜져 있으면
            # max_tokens는 thinking+본문 합계 상한이라는 점을 특히 주의.
            log.warning("%s 응답이 max_tokens(%d)에서 잘렸습니다.", model, max_tokens)
        return "".join(b.text for b in resp.content if b.type == "text")

    def json(self, model: str, system: str, user: str, max_tokens: int = 1024,
             **params):
        """JSON 응답을 기대하는 호출. 파싱 실패 시 빈 dict.

        ⚠️ **Sonnet 계열에 params 없이 부르면 최고 설정으로 돈다.** thinking을
        생략하면 adaptive thinking이 켜지고 effort 기본값은 high다 — 즉 아무것도
        넘기지 않는 것은 "기본값"이 아니라 "가장 비싼 값"을 고르는 것이다. 게다가
        max_tokens는 thinking과 본문의 합계 상한이라, thinking이 예산을 먹으면
        JSON이 잘리고 여기서는 그냥 빈 dict로 보인다 — 호출자 눈에는 모델이 빈
        응답을 준 것과 구분되지 않는다.

        그래서 params를 뚫어 둔다. Sonnet으로 부를 거면 thinking과 effort를
        **명시**할 것. Haiku 4.5는 둘 다 받지 않으므로(400) 넘기지 않는다.
        """
        return self._loads(
            self.text(model, system, user, max_tokens=max_tokens, **params))

    def json_with_images(self, model: str, system: str, images: list[bytes],
                         user: str, max_tokens: int = 2000) -> dict:
        """페이지 그림 + 지시문 → JSON.

        **텍스트 레이어가 없는 PDF(스캔본)에서 쓴다.** PDF를 document 블록으로
        통째로 넘기지 않고 호출자가 렌더한 페이지만 보내는 이유는 비용이다 —
        document 블록은 60페이지짜리면 60장을 전부 이미지로 만든다. 제목과 초록은
        앞 한두 장에만 있다.

        Haiku 4.5는 구세대라 effort/thinking을 받지 않는다(400). 넘기지 않는다.
        """
        import base64

        content: list[dict] = [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png",
                        # standard_b64encode는 줄바꿈을 넣지 않는다 (API 요구사항).
                        "data": base64.standard_b64encode(png).decode()}}
            for png in images]
        content.append({"type": "text", "text": user})

        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": content}])
        self._log_usage(model, resp)
        if resp.stop_reason == "max_tokens":
            log.warning("%s 비전 응답이 max_tokens(%d)에서 잘렸습니다.",
                        model, max_tokens)
        return self._loads("".join(b.text for b in resp.content if b.type == "text"))

    @staticmethod
    def _loads(text: str) -> dict:
        """모델이 뱉은 텍스트에서 JSON을 꺼낸다. 실패하면 빈 dict."""
        text = text.strip()
        if text.startswith("```"):          # 코드펜스 제거
            text = text.split("```")[1].removeprefix("json").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("JSON 파싱 실패: %s", _shape(text))
            return {}

    def structured_with_pdf(self, model: str, system: str, pdf_bytes: bytes,
                            user: str, schema: dict, max_tokens: int,
                            **params) -> dict:
        """PDF 원본 + 텍스트를 넘기고 스키마에 맞는 JSON을 받는다.

        **PDF는 맨 앞에 온다** — 공식 권장 배치가 문서 먼저다.

        ⚠️ **cache_control을 걸지 않는다. 다시 넣지 말 것.**
        원래 설계는 이 PDF를 두 번째 호출(종합)이 ~0.1×로 재사용하는 것을 전제로
        캐시를 걸었는데, 구현된 종합(graph/nodes.py `_summary`)은 PDF를 아예 넘기지
        않는다 — 선정 5편의 리뷰 원문만 있으면 되기 때문이다. 그래서 캐시는 **쓰기
        프리미엄 1.25×만 내고 한 번도 읽히지 않았다** (26p PDF 기준 분석 1회당
        입력 토큰 약 20%가 그대로 버려졌다).

        되살릴 조건은 하나뿐이다: 같은 PDF를 5분(기본 TTL) 안에 **두 번 이상**
        넘기는 호출 경로가 실제로 생길 때. 그때 `_log_usage`의
        cache_read_input_tokens가 0이 아닌지로 검증하고 넣을 것.

        ⚠️ **스키마로 배열 길이를 강제할 수 없다.** 구조화 출력은 maxItems 같은
        배열 제약을 지원하지 않으므로, "최대 5편"은 호출자가 코드에서 잘라야 한다.

        JSON 파싱은 실패할 수 없다 — output_config.format이 형식을 보장한다. 다만
        max_tokens에서 잘리면(stop_reason=max_tokens) 불완전한 JSON이 오므로 그
        경우만 빈 dict를 돌려준다.
        """
        import base64

        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        # standard_b64encode는 줄바꿈을 넣지 않는다 (API 요구사항).
                        "data": base64.standard_b64encode(pdf_bytes).decode(),
                    },
                },
                {"type": "text", "text": user},
            ]}],
            output_config={"format": {"type": "json_schema", "schema": schema},
                           **params.pop("output_config", {})},
            **params,
        )
        self._log_usage(model, resp)
        if resp.stop_reason == "max_tokens":
            log.warning("%s 응답이 max_tokens(%d)에서 잘려 JSON이 불완전합니다.",
                        model, max_tokens)
            return {}
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 구조화 출력이 보장하므로 여기 오면 안 된다. 와도 검색 결과는 살린다.
            log.warning("구조화 출력인데 JSON 파싱 실패: %s", _shape(text))
            return {}

    @staticmethod
    def _log_usage(model: str, resp) -> None:
        """토큰 사용량을 남긴다 — 비용이 추정이 아니라 실측으로 남게.

        **모든 호출 경로가 이걸 통과해야 한다.** 한 곳이라도 빠지면 그 단계의 비용만
        장부에서 사라지고, 합계가 맞지 않는 이유를 나중에 찾기 어렵다.

        cache_write/cache_read는 **현재 둘 다 0이 정상이다** — 프롬프트 캐싱을 쓰는
        경로가 없다(structured_with_pdf 주석 참고). 0이 아닌 값이 보이기 시작하면
        누군가 cache_control을 다시 넣었다는 뜻이고, 그때는 read가 따라 올라오는지를
        같이 봐야 한다. write만 늘고 read가 0이면 비용만 1.25배가 된다.
        """
        u = getattr(resp, "usage", None)
        if u is None:
            return
        log.info("%s usage: in=%s out=%s cache_write=%s cache_read=%s",
                 model, u.input_tokens, u.output_tokens,
                 getattr(u, "cache_creation_input_tokens", None),
                 getattr(u, "cache_read_input_tokens", None))


def get_llm(enabled: bool | None = None) -> ClaudeLLM | None:
    """LLM 인스턴스 또는 None.

    enabled=None이면 설정값 PAPER_ASSISTANT_USE_LLM을 따른다 (기본 off).
    off면 노드가 스텁 출력을 만든다 → 크레딧 소비 없음.
    """
    if enabled is None:
        enabled = config.USE_LLM
    return ClaudeLLM() if enabled else None
