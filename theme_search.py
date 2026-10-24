"""Pure intent ranking with normalization and fuzzy matches reused per call."""
from difflib import SequenceMatcher
import re

import pandas as pd


def _normalize_search_text(value):
    return re.sub(r"[^0-9a-z가-힣]+", "", str(value or "").lower())


def _prepare_field(field_text):
    tokens = tuple(token for token in re.findall(r"[0-9a-z가-힣]+", field_text) if len(token) >= 2)
    return _normalize_search_text(field_text), tokens


def _match_multiplier(term_norm, field_norm, field_tokens, token_ratios=None):
    if not term_norm or not field_norm:
        return 0.0
    if term_norm in field_norm:
        return 1.0
    best = 0.0
    for token in field_tokens:
        # These tokens already contain only the normalization alphabet.
        # SequenceMatcher cannot match more than the shorter token's length.
        if 2.0 * min(len(term_norm), len(token)) / (len(term_norm) + len(token)) < 0.76:
            continue
        key = (term_norm, token)
        ratio = token_ratios.get(key) if token_ratios is not None else None
        if ratio is None:
            ratio = SequenceMatcher(None, term_norm, token).ratio()
            if token_ratios is not None:
                token_ratios[key] = ratio
        best = max(best, ratio)
    if best >= 0.86:
        return 0.55
    if best >= 0.76:
        return 0.25
    return 0.0


def _weighted_score(multiplier, exact_weight):
    if multiplier == 1.0:
        return float(exact_weight)
    return exact_weight * multiplier if multiplier else 0.0


def _search_term_score(term, field_value, exact_weight):
    """Keep the original single-field scoring behavior for standalone callers."""
    term_text = str(term or "").strip().lower()
    field_text = str(field_value or "").strip().lower()
    if len(term_text) < 2 or not field_text:
        return 0.0
    term_norm = _normalize_search_text(term_text)
    field_norm, field_tokens = _prepare_field(field_text)
    return _weighted_score(_match_multiplier(term_norm, field_norm, field_tokens), exact_weight)


def rank_themes_by_ai_intent(themes, intent):
    if not intent:
        return themes.copy()

    weighted_terms = []
    for key, weight in [
        ("genres", 12),
        ("subjects", 11),
        ("narrative_elements", 10),
        ("moods", 8),
        ("positive_keywords", 8),
        ("anchor_titles", 3),
    ]:
        for value in intent.get(key, []) or []:
            if str(value).strip():
                term = str(value).strip()
                term_text = str(term or "").strip().lower()
                term_norm = _normalize_search_text(term_text)
                weighted_terms.append((term, term_norm, len(term_text) >= 2, (
                    weight * 1.35, weight * 1.15, weight * 1.25,
                    weight, weight * 0.85, weight * 0.55,
                )))

    negative_terms = [str(value).strip() for value in (intent.get("negative_keywords", []) or []) if str(value).strip()]
    negative_norms = [_normalize_search_text(term) for term in negative_terms]
    # Both indexes belong to this invocation, so edited rows are always current.
    prepared_fields = {}
    match_multipliers = {}
    token_ratios = {}
    scored_rows = []

    for _, row in themes.iterrows():
        fields = (
            row.get("theme_name", ""), row.get("copy", ""),
            row.get("trigger_keywords", ""), row.get("genre", ""),
            row.get("mood", ""), row.get("source_issue", ""),
        )
        normalized_fields = []
        if weighted_terms:
            for value in fields:
                field_text = str(value or "").strip().lower()
                prepared = prepared_fields.get(field_text)
                if prepared is None:
                    prepared = _prepare_field(field_text)
                    prepared_fields[field_text] = prepared
                normalized_fields.append((field_text, *prepared))

        score = 0.0
        matched = []
        matched_norms = set()
        for term, term_norm, long_enough, weights in weighted_terms:
            field_scores = []
            for weight, (field_text, field_norm, field_tokens) in zip(weights, normalized_fields):
                if not long_enough or not field_text or not term_norm:
                    field_scores.append(0.0)
                    continue
                key = (term_norm, field_text)
                multiplier = match_multipliers.get(key)
                if multiplier is None:
                    multiplier = _match_multiplier(term_norm, field_norm, field_tokens, token_ratios)
                    match_multipliers[key] = multiplier
                field_scores.append(_weighted_score(multiplier, weight))
            term_score = max(field_scores)
            if term_score > 0:
                score += term_score
                matched.append(term)
                matched_norms.add(term_norm)

        combined_norm = _normalize_search_text(" ".join(str(value) for value in fields).lower())
        for negative_norm in negative_norms:
            if negative_norm in combined_norm:
                score -= 25

        score += min(18, len(matched_norms) * 3)

        if score > 0:
            item = row.copy()
            item["ai_search_score"] = round(score, 2)
            item["ai_matched_terms"] = ",".join(dict.fromkeys(matched))
            scored_rows.append(item)

    if not scored_rows:
        return themes.iloc[0:0].copy()

    result = pd.DataFrame(scored_rows)
    return result.sort_values(["ai_search_score", "theme_name"], ascending=[False, True])
