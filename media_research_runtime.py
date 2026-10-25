"""Explicit business-research action; no collection or paid calls on reading."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy


def with_project_evidence(snapshot, project, question=""):
    """Reuse saved original excerpts, never past AI prose as public evidence."""
    from media_insights_analysis import _url
    from media_research_engine import recalled_project_turns

    result = deepcopy(snapshot) if isinstance(snapshot, Mapping) else {"items": [], "sources": []}
    items = result.setdefault("items", [])
    known = {item.get("id") for item in items if isinstance(item, Mapping)}
    references = {}
    for turn in recalled_project_turns(project, question):
        answer = turn.get("answer", {}) if isinstance(turn, Mapping) else {}
        for source in answer.get("sources", [])[:40] if isinstance(answer, Mapping) else []:
            if not isinstance(source, Mapping):
                continue
            identifier, evidence_id = source.get("document_id"), source.get("id")
            url, excerpt, content_hash = _url(source.get("url")), source.get("excerpt"), source.get("content_hash")
            if (not isinstance(identifier, str) or not identifier or identifier in known
                    or not isinstance(evidence_id, str) or not evidence_id
                    or not url or not isinstance(excerpt, str) or len(excerpt.strip()) < 12
                    or not isinstance(content_hash, str) or not content_hash):
                continue
            current = references.get(identifier)
            if current is None or current["content_hash"] != content_hash:
                current = references[identifier] = {
                    "id": identifier, "title": str(source.get("title", ""))[:500],
                    "company": str(source.get("company", ""))[:120],
                    "topic": str(source.get("topic", ""))[:120],
                    "url": url, "content_hash": content_hash,
                    "published_at": source.get("published_at") or "",
                    "collected_at": source.get("collected_at") or "",
                    "reference_checked_at": source.get("reference_checked_at") or source.get("collected_at") or "",
                    "source_type": "research_reference", "research_reference": True,
                    "from_project_history": True,
                    "country": source.get("country") or "", "evidence": [],
                }
            if all(row["id"] != evidence_id for row in current["evidence"]):
                current["evidence"].append({"id": evidence_id, "text": excerpt[:4000],
                                            "url": url, "locator": str(source.get("locator", ""))[:200]})
    items.extend(list(references.values())[-48:])
    return result


def answer_research(question, snapshot, project, *, api_key, model):
    """Fresh sources + saved context + one structured strategy response."""
    from media_research_engine import answer_business_question
    from media_research_sources import enrich_research_sources

    contextual = with_project_evidence(snapshot, project, question)
    enriched = enrich_research_sources(question, contextual, api_key=api_key, model=model)
    answer = answer_business_question(question, enriched["snapshot"], project, api_key=api_key, model=model)
    answer["source_search"] = deepcopy(enriched["report"])
    answer.setdefault("retrieval", {})["live_web_search"] = enriched["report"].get("documents_added", 0) > 0
    return answer
