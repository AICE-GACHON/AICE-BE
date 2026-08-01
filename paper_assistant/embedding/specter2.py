"""SPECTER2 논문 임베딩.

논문 1편 = 벡터 1개. 청킹하지 않는다 — SPECTER2가 title+abstract 수준의
paper-level 표현을 학습한 모델이라, 본문을 쪼개 넣으면 오히려 노이즈가 된다.

입력 형식은 학습 시와 동일하게 `title + [SEP] + abstract`를 지켜야 한다.
임베딩은 마지막 레이어의 CLS 토큰.

adapter 종류 (같은 base 위에 갈아끼움):
- proximity : 논문↔논문 유사도 검색 (본 프로젝트의 용도)
- adhoc_query: 짧은 키워드 쿼리 → 논문 검색
"""
import logging

import torch
from adapters import AutoAdapterModel
from transformers import AutoTokenizer

BASE_MODEL = "allenai/specter2_base"
ADAPTERS = {
    "proximity": "allenai/specter2",
    "adhoc_query": "allenai/specter2_adhoc_query",
}
MAX_LENGTH = 512

log = logging.getLogger(__name__)


class Specter2Embedder:
    def __init__(self, adapter: str = "proximity", device: str | None = None):
        if adapter not in ADAPTERS:
            raise ValueError(f"알 수 없는 adapter: {adapter} (가능: {list(ADAPTERS)})")
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        log.info("SPECTER2 로드 중 (adapter=%s, device=%s)...", adapter, self.device)

        self.tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
        self.model = AutoAdapterModel.from_pretrained(BASE_MODEL)
        self.model.load_adapter(ADAPTERS[adapter], source="hf",
                                load_as=adapter, set_active=True)
        self.model.to(self.device).eval()
        self.dim = self.model.config.hidden_size
        log.info("로드 완료 — 임베딩 차원 %d", self.dim)

    def _format(self, title: str, abstract: str) -> str:
        return f"{title or ''}{self.tokenizer.sep_token}{abstract or ''}"

    @torch.no_grad()
    def encode(self, papers: list[tuple[str, str]], batch_size: int = 16):
        """(title, abstract) 리스트 → (N, dim) 텐서.

        반환 벡터는 L2 정규화되어 있다. pgvector에서 코사인 거리를 쓸 때
        정규화된 벡터끼리는 내적과 동일해져 계산이 단순해진다.
        """
        vectors = []
        for i in range(0, len(papers), batch_size):
            batch = papers[i:i + batch_size]
            texts = [self._format(t, a) for t, a in batch]
            inputs = self.tokenizer(
                texts, padding=True, truncation=True, max_length=MAX_LENGTH,
                return_tensors="pt", return_token_type_ids=False,
            ).to(self.device)
            output = self.model(**inputs)
            cls = output.last_hidden_state[:, 0, :]  # CLS 토큰
            vectors.append(torch.nn.functional.normalize(cls, p=2, dim=1).cpu())
        return torch.cat(vectors) if vectors else torch.empty(0, self.dim)

    def encode_one(self, title: str, abstract: str):
        return self.encode([(title, abstract)])[0]


# --- 검색 결과 신뢰도 (쿼리 단위) -------------------------------------------
#
# 논문 사이는 못 가르지만 **쿼리 사이는 아주 잘 갈린다** (설계서 §20 실측).
# top-5 평균 코사인:
#   도메인 안(GNN·LSTM·Transformer·LoRA·diffusion·FL)  0.9457 ~ 0.9664
#   도메인 밖(치즈미생물학·바흐·안데스지질·무릎수술·한자동맹) 0.8522 ~ 0.8668
# 겹치는 구간이 전혀 없다. 경계는 무작위쌍 분포의 분위수에 맞춰 잡는다
# (0.8998 = 95분위, 0.9231 = 99분위).
#
# 이 판정이 없으면 "치즈 숙성 미생물학"을 넣어도 ML 논문 20편을 자신있게
# 보여주는 게 현재의 최악 실패 모드다.

STRONG_THRESHOLD = 0.93     # 무작위쌍 99분위 초과
MODERATE_THRESHOLD = 0.90   # 무작위쌍 95분위

CONFIDENCE_MESSAGES = {
    "strong": "이 주제의 논문이 코퍼스에 충분히 있습니다.",
    "moderate": "직접 대응하는 논문은 적고, 인접 분야 위주로 매칭됐습니다. "
                "결과를 참고 수준으로 보세요.",
    "weak": "코퍼스(ICLR·NeurIPS)에 이 주제의 논문이 사실상 없습니다. "
            "아래 결과는 무작위 논문과 다를 바 없으니 신뢰하지 마세요.",
}


def retrieval_confidence(cosines: list[float], k: int = 5) -> tuple[str, float]:
    """상위 코사인들로 (신뢰도 등급, 근거값) 판정.

    cosines: 벡터 검색 상위 코사인 (내림차순). 비어 있으면 ('weak', 0.0).
    반환값의 근거값은 top-k 평균 코사인 — 진단용이며 사용자에게 노출하지 않는다.
    """
    usable = [c for c in cosines if c is not None][:k]
    if not usable:
        return "weak", 0.0
    score = sum(usable) / len(usable)
    if score >= STRONG_THRESHOLD:
        return "strong", score
    if score >= MODERATE_THRESHOLD:
        return "moderate", score
    return "weak", score
