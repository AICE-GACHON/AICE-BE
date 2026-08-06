"""Claude API 래퍼 (예산 제약 인지).

- 태깅/추출: Haiku 4.5 (저가)
- 종합 리포트: Sonnet 5 (품질)

예산이 빠듯해서 LLM 호출은 명시적으로 켤 때만 실행된다.
`get_llm(enabled=False)`(기본)는 None을 반환하고, 노드는 이때 결정론적
스텁 출력을 생성한다 — DAG 배선을 $0으로 검증하기 위함.
"""
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

# --- 재정렬은 종합과 반대 성격이라 값이 다르다 ---------------------------------
# 종합에서 LLM은 "코드가 검증한 사실을 문장으로 옮기는" 역할이라 effort를 낮췄다.
# 재정렬에서는 LLM이 **판정자 본인**이다 — 후보 50편과 논문 원문을 읽고 무엇이
# 정말 비슷한지를 결정하는 일이고, 이 파이프라인에서 임베딩이 못 하는 바로 그
# 일이다. 여기서 아끼면 개편의 근거 자체가 없어진다.
RERANK_EFFORT = "high"
# thinking과 본문의 **합계** 상한이다. 후보 50편을 견주는 추론이라 thinking이
# 길고, 본문도 5편분 선정 이유가 들어간다. 잘리면 조용히 JSON이 깨진다.
RERANK_MAX_TOKENS = 8000


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
        if resp.stop_reason == "max_tokens":
            # 조용히 잘린 응답은 알아채기 어렵다. thinking이 켜져 있으면
            # max_tokens는 thinking+본문 합계 상한이라는 점을 특히 주의.
            log.warning("%s 응답이 max_tokens(%d)에서 잘렸습니다.", model, max_tokens)
        return "".join(b.text for b in resp.content if b.type == "text")

    def json(self, model: str, system: str, user: str, max_tokens: int = 1024):
        """JSON 응답을 기대하는 호출. 파싱 실패 시 빈 dict."""
        text = self.text(model, system, user, max_tokens=max_tokens).strip()
        if text.startswith("```"):          # 코드펜스 제거
            text = text.split("```")[1].removeprefix("json").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("JSON 파싱 실패: %s", text[:120])
            return {}

    def structured_with_pdf(self, model: str, system: str, pdf_bytes: bytes,
                            user: str, schema: dict, max_tokens: int,
                            **params) -> dict:
        """PDF 원본 + 텍스트를 넘기고 스키마에 맞는 JSON을 받는다.

        **PDF는 반드시 맨 앞에 온다.** 두 가지 이유가 겹친다:
        (1) 공식 권장 배치가 문서 먼저이고,
        (2) 프롬프트 캐싱은 **프리픽스 일치**라, PDF 앞에 가변 내용이 하나라도
            들어가면 캐시가 통째로 죽는다. 같은 PDF로 두 번째 호출(종합)을 할 때
            PDF 부분을 ~0.1× 로 재사용하는 것이 이 구조의 비용 전제다.

        cache_control을 PDF 블록에 건다. 뒤따르는 user 텍스트(후보 목록)는 호출마다
        달라지므로 캐시 경계 뒤에 둔다.

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
                    "cache_control": {"type": "ephemeral"},
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
            log.warning("구조화 출력인데 JSON 파싱 실패: %s", text[:200])
            return {}

    @staticmethod
    def _log_usage(model: str, resp) -> None:
        """토큰 사용량을 남긴다 — 비용이 추정이 아니라 실측으로 남게.

        cache_read_input_tokens가 계속 0이면 프리픽스가 매번 달라지고 있다는 뜻이라,
        캐싱을 전제로 짠 비용 계산이 틀어진다. 조용히 비싸지는 종류의 문제다.
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
