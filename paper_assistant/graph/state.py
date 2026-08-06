"""LangGraph 파이프라인 상태.

노드들이 순차/병렬로 채워 나가는 공유 상태. 각 노드는 자신이 담당하는
키만 반환하면 LangGraph가 병합한다 (병렬 노드는 서로 다른 키를 쓰므로 충돌 없음).
"""
from typing import TypedDict

from paper_assistant.retrieval.hybrid_search import SearchResult
from paper_assistant.schemas import (
    PaperSelection, Report, ResubmissionFlow, RetrievalConfidence, ReviewPattern,
    SimilarityTag, VenueTrend)


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

    # --- 병렬 분석 4종 ---
    selections: list[PaperSelection]     # llm_rerank_node

    # --- 병렬 분석 3종 (기존) ---
    similarity_tags: dict[int, list[SimilarityTag]]   # paper_id -> tags
    review_patterns: list[ReviewPattern]
    venue_trends: list[VenueTrend]
    resubmission_flows: list[ResubmissionFlow]
    paper_ratings: dict[int, dict]   # paper_id -> {"avg", "count", "spread"}

    # --- 종합 (synthesis_node) ---
    report: Report
