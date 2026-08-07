"""LangGraph 파이프라인 상태.

노드들이 순서대로 채워 나가는 공유 상태. 각 노드는 자신이 담당하는 키만 반환하면
LangGraph가 병합한다.
"""
from typing import TypedDict

from paper_assistant.retrieval.hybrid_search import SearchResult
from paper_assistant.schemas import (
    PaperSelection, Report, RetrievalConfidence, SelectedPaper)


class PipelineState(TypedDict, total=False):
    # --- 입력 (input_node) ---
    query_title: str
    query_abstract: str
    pdf_bytes: bytes | None

    # --- 검색 (retrieval_node) ---
    query_embedding: list
    similar_papers: list[SearchResult]
    # 검색 단계에서 판정한다. 재정렬이 이 값을 보고 돌릴지 말지 정하므로 종합까지
    # 미룰 수 없다 — weak이면 LLM을 부르지 않는다(비용도, 잘못된 결과도 아낀다).
    confidence: RetrievalConfidence

    # --- 2단계 선정 (llm_rerank → review_fetch) ---
    selections: list[PaperSelection]          # LLM의 원출력 (내부용)
    selected_papers: list[SelectedPaper]      # 리뷰까지 붙인 것 (Report에 실린다)
    selection_points: dict[int, list[dict]]   # 근거 풀 재료 (paper_id -> 지적 문장)

    # --- 종합 (synthesis_node) ---
    report: Report
