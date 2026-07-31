from __future__ import annotations

import re
from typing import Any, Iterable


def _tokens(value: Any) -> set[str]:
    return {
        token for token in re.findall(r"[0-9a-z가-힣]+", str(value or "").lower())
        if len(token) >= 2
    }


def _block_text(block: dict[str, Any]) -> str:
    return " ".join(
        str(block.get(key, ""))
        for key in ["name", "title", "block_type", "text", "genre", "theme", "content_titles"]
    )


def _theme_text(theme: dict[str, Any]) -> str:
    return " ".join(
        str(theme.get(key, ""))
        for key in ["theme_name", "copy", "genre", "mood", "trigger_keywords"]
    )


def _overlap_score(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


def build_insertion_slots(blocks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        [dict(block) for block in blocks if bool(block.get("visible", True))],
        key=lambda block: float(block.get("y", block.get("order", 0)) or 0),
    )
    slots: list[dict[str, Any]] = []
    for index in range(len(ordered) + 1):
        before = ordered[index - 1] if index > 0 else None
        after = ordered[index] if index < len(ordered) else None
        slots.append({
            "slot_index": index,
            "after_block": before or {},
            "before_block": after or {},
            "y": float((after or before or {}).get("y", index) or index),
        })
    return slots


def recommend_theme_positions(
    theme: dict[str, Any],
    blocks: Iterable[dict[str, Any]],
    *,
    viewport_height: float = 1080,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Rank insertion slots for a generated theme using an extension-captured UI snapshot."""
    theme_text = _theme_text(theme)
    results: list[dict[str, Any]] = []
    for slot in build_insertion_slots(blocks):
        before = slot["after_block"]
        after = slot["before_block"]
        before_text = _block_text(before)
        after_text = _block_text(after)
        adjacent_relevance = max(
            _overlap_score(theme_text, before_text),
            _overlap_score(theme_text, after_text),
        )

        nearby_similarity = max(
            _overlap_score(theme_text, before_text),
            _overlap_score(theme_text, after_text),
        )
        duplicate_penalty = 0.0
        if nearby_similarity > 0.65:
            duplicate_penalty = 18.0
        elif nearby_similarity > 0.4:
            duplicate_penalty = 8.0

        y = float(slot.get("y", 0) or 0)
        visibility = 20.0 if y <= viewport_height else max(4.0, 20.0 - (y - viewport_height) / 250)

        journey_bonus = 0.0
        before_name = before_text.lower()
        after_name = after_text.lower()
        if any(token in before_name for token in ["ai 추천", "맞춤 추천", "인기", "랭킹", "히어로", "빅배너"]):
            journey_bonus += 13.0
        if any(token in after_name for token in ["장르", "전체", "키즈", "설정", "푸터"]):
            journey_bonus += 8.0
        if any(token in before_name for token in ["푸터", "설정"]):
            journey_bonus -= 15.0

        score = adjacent_relevance * 35.0 + visibility + journey_bonus - duplicate_penalty
        after_label = before.get("name") or before.get("title") or "화면 상단"
        before_label = after.get("name") or after.get("title") or "화면 하단"
        reason_parts = []
        if y <= viewport_height:
            reason_parts.append("첫 화면 노출 가능")
        if journey_bonus > 0:
            reason_parts.append("추천 탐색 흐름과 연결")
        if duplicate_penalty:
            reason_parts.append("인접 블록과 소재 중복 주의")
        if adjacent_relevance > 0.25:
            reason_parts.append("주변 블록과 문맥 연결")
        if not reason_parts:
            reason_parts.append("주변 블록과의 충돌이 적음")

        results.append({
            "score": round(score, 1),
            "slot_index": slot["slot_index"],
            "placement": f"'{after_label}' 다음 · '{before_label}' 이전",
            "reason": " · ".join(reason_parts),
            "after_block": before,
            "before_block": after,
        })

    return sorted(results, key=lambda item: item["score"], reverse=True)[:max(1, top_k)]
