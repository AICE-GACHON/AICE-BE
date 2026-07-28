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


class ClaudeLLM:
    def __init__(self):
        import anthropic   # 지연 import — LLM 끌 땐 설치/키 불필요
        # API 키는 anthropic SDK가 ANTHROPIC_API_KEY 환경변수에서 직접 읽는다.
        self.client = anthropic.Anthropic()

    def text(self, model: str, system: str, user: str, max_tokens: int = 2048) -> str:
        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return next((b.text for b in resp.content if b.type == "text"), "")

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
