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


def get_llm(enabled: bool | None = None) -> ClaudeLLM | None:
    """LLM 인스턴스 또는 None.

    enabled=None이면 설정값 PAPER_ASSISTANT_USE_LLM을 따른다 (기본 off).
    off면 노드가 스텁 출력을 만든다 → 크레딧 소비 없음.
    """
    if enabled is None:
        enabled = config.USE_LLM
    return ClaudeLLM() if enabled else None
