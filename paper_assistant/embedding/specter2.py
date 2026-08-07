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
import os

import torch
from adapters import AutoAdapterModel
from huggingface_hub import constants as hf_constants
from huggingface_hub import errors as hf_errors
from transformers import AutoTokenizer

BASE_MODEL = "allenai/specter2_base"
ADAPTERS = {
    "proximity": "allenai/specter2",
    "adhoc_query": "allenai/specter2_adhoc_query",
}
MAX_LENGTH = 512

log = logging.getLogger(__name__)


class Specter2LoadError(RuntimeError):
    """SPECTER2(base 또는 adapter) 로드 실패.

    이 메시지는 app/services/analysis.py가 prediction.error에 그대로 적고
    프론트 에러 박스에 노출된다 — 원인과 다음 행동이 한 문장에 들어가야 한다.
    """


def _root_cause(exc: BaseException) -> BaseException:
    """예외 체인의 가장 깊은 원인을 찾는다.

    adapters는 실패 원인을 삼킨다 — utils.py의 resolve_adapter_path()가
    `except Exception as ex: raise EnvironmentError("Unable to load adapter ...
    from any source. Please check the name of the adapter or the source.")`로
    끝나기 때문에, 표면 메시지는 네트워크 단절이든 캐시 손상이든 항상 똑같고
    "이름을 확인하라"고만 한다(실제로는 이름이 멀쩡한 경우가 대부분이다).

    다만 저 raise가 except 블록 안에 있어서 파이썬이 __context__를 자동으로
    걸어 둔다. `raise ... from ex`가 없어도 진짜 원인은 체인에 남아 있다.
    """
    seen = {id(exc)}
    cause = exc
    while True:
        nxt = cause.__cause__ or cause.__context__
        if nxt is None or id(nxt) in seen:
            return cause
        seen.add(id(nxt))
        cause = nxt


def _status_code(exc: BaseException) -> int | None:
    """HTTP 계열 예외에서 상태 코드를 꺼낸다 (없으면 None)."""
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def _diagnose(exc: BaseException, label: str, repo_id: str) -> str:
    """원인 예외를 보고 사람이 행동할 수 있는 메시지로 바꾼다.

    label은 사람이 읽을 이름("SPECTER2 base 모델(...)"), repo_id는 캐시 경로를
    만들 때 쓰는 실제 저장소 id다 — 둘을 섞으면 안내하는 경로가 엉뚱해진다.
    """
    cause = _root_cause(exc)
    detail = f"{type(cause).__name__}: {cause}".strip()
    status = _status_code(cause)

    if isinstance(cause, (hf_errors.OfflineModeIsEnabled, hf_errors.LocalEntryNotFoundError)) \
            or hf_constants.HF_HUB_OFFLINE:
        return (
            f"{label}을(를) 받을 수 없습니다 — 오프라인 모드이거나 Hugging Face Hub에 "
            f"연결할 수 없고, 로컬 캐시({hf_constants.HF_HUB_CACHE})에도 완전한 사본이 없습니다. "
            f"네트워크 연결을 확인하세요. HF_HUB_OFFLINE이 켜져 있다면 끄고 다시 시도하세요. "
            f"(원인: {detail})"
        )

    # HF는 없는 저장소에도 존재 여부를 숨기려고 401을 준다 — 404/403과 함께 묶어
    # "이름이 틀렸거나 권한이 없다"로 안내해야 실제 상황과 맞는다.
    if isinstance(cause, (hf_errors.RepositoryNotFoundError, hf_errors.RevisionNotFoundError,
                          hf_errors.GatedRepoError, hf_errors.EntryNotFoundError)) \
            or status in (401, 403, 404):
        return (
            f"Hugging Face Hub에서 {label}을(를) 찾을 수 없거나 접근 권한이 없습니다. "
            f"저장소 이름({repo_id})이 맞는지, 비공개/게이트 저장소라면 HF_TOKEN이 "
            f"설정돼 있는지 확인하세요. (원인: {detail})"
        )

    if isinstance(cause, (hf_errors.HfHubHTTPError, OSError)):
        return (
            f"{label} 로드에 실패했습니다 — 다운로드가 중간에 끊겼거나 캐시가 손상됐을 수 있습니다. "
            f"네트워크를 확인하고 다시 시도하세요. 반복되면 캐시를 지우고 다시 받으세요: "
            f"{os.path.join(hf_constants.HF_HUB_CACHE, _cache_dirname(repo_id))} "
            f"(원인: {detail})"
        )

    return f"{label} 로드에 실패했습니다. (원인: {detail})"


def _cache_dirname(repo_id: str) -> str:
    """'allenai/specter2' -> 'models--allenai--specter2' (HF 캐시 디렉터리명 규칙)."""
    return "models--" + repo_id.replace("/", "--")


class Specter2Embedder:
    def __init__(self, adapter: str = "proximity", device: str | None = None):
        if adapter not in ADAPTERS:
            raise ValueError(f"알 수 없는 adapter: {adapter} (가능: {list(ADAPTERS)})")
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        log.info("SPECTER2 로드 중 (adapter=%s, device=%s)...", adapter, self.device)

        # base와 adapter를 따로 감싼다 — 440MB짜리 base와 3.5MB짜리 adapter는
        # 실패 양상도 복구 방법도 달라서, 어느 쪽이 죽었는지가 곧 진단이다.
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
            self.model = AutoAdapterModel.from_pretrained(BASE_MODEL)
        except Exception as exc:
            log.exception("SPECTER2 base 로드 실패 (%s)", BASE_MODEL)
            raise Specter2LoadError(
                _diagnose(exc, f"SPECTER2 base 모델({BASE_MODEL})", BASE_MODEL)) from exc

        repo_id = ADAPTERS[adapter]
        try:
            self.model.load_adapter(repo_id, source="hf",
                                    load_as=adapter, set_active=True)
        except Exception as exc:
            log.exception("SPECTER2 adapter 로드 실패 (%s)", repo_id)
            raise Specter2LoadError(
                _diagnose(exc, f"SPECTER2 adapter({repo_id})", repo_id)) from exc

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
