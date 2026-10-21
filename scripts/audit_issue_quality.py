#!/usr/bin/env python3
"""Compare the original issue ranking with the current quality gates, offline.

This reads frozen repository data, never runs a collector or an AI request, and
does not update application CSVs. A successful run is a regression comparison,
not independent verification of current public rankings or articles.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import date
from difflib import SequenceMatcher
import hashlib
import html
from io import BytesIO
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
BASELINE_REF = "1e40feecbfa9894443a6f145337fa7d1d0e43328"
AS_OF = "2026-10-21"
DATA_FILES = ("issue_feed.csv", "youtube_video_watchlist.csv", "youtube_video_stats.csv")


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def baseline_file(name: str, args: argparse.Namespace) -> bytes:
    if args.baseline_dir:
        return (args.baseline_dir / f"before_{name}").read_bytes()
    try:
        return subprocess.check_output(
            ["git", "show", f"{args.baseline_ref}:{name}"], cwd=ROOT,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "The original baseline commit is unavailable locally. Fetch that "
            "commit first, or pass --baseline-dir containing before_app.py and "
            "before_issue_feed.csv. No network fetch was attempted."
        ) from exc


class FreezeToday(ast.NodeTransformer):
    def __init__(self, as_of: str):
        self.as_of = as_of

    def visit_Call(self, node):
        node = self.generic_visit(node)
        func = node.func
        if (
            isinstance(func, ast.Attribute) and func.attr == "today"
            and isinstance(func.value, ast.Attribute) and func.value.attr == "Timestamp"
            and isinstance(func.value.value, ast.Name) and func.value.value.id == "pd"
        ):
            return ast.copy_location(
                ast.Call(
                    func=ast.Attribute(value=ast.Name(id="pd", ctx=ast.Load()), attr="Timestamp", ctx=ast.Load()),
                    args=[ast.Constant(value=self.as_of)], keywords=[],
                ), node,
            )
        return node


def original_ranking(source: str, feed: pd.DataFrame, as_of: str):
    """Load only function definitions, avoiding Streamlit startup and secrets."""
    tree = ast.parse(source)
    namespace = dict(
        pd=pd, re=re, math=math, html=html, Path=Path,
        parse_qs=parse_qs, urlparse=urlparse, SequenceMatcher=SequenceMatcher,
    )
    config = next(
        node for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "SOURCE_CONFIG" for target in node.targets)
    )
    namespace["SOURCE_CONFIG"] = ast.literal_eval(config.value)
    namespace["SOURCE_WEIGHTS"] = {
        name: value["weight_pct"] for name, value in namespace["SOURCE_CONFIG"].items()
    }
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for function in functions:
        function.decorator_list = []
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + functions,
        type_ignores=[],
    )
    module = FreezeToday(as_of).visit(module)
    exec(compile(ast.fix_missing_locations(module), "frozen_original_app.py", "exec"), namespace)
    recent, start, end = namespace["filter_recent_issues"](feed)
    prepared = namespace["prepare_issues"](recent)
    ranked = namespace["build_core_issues"](prepared)
    return recent, prepared, ranked, start, end


def plain(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(plain(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def cell(value, limit=180) -> str:
    value = re.sub(r"\s+", " ", plain(value)).strip()
    if len(value) > limit:
        value = value[:limit - 1] + "…"
    return value.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


class MarkdownCell(str):
    """Already escaped table content, used to preserve complete source links."""


def markdown_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(str(value) if isinstance(value, MarkdownCell) else cell(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def source_counts(frame: pd.DataFrame, field="source") -> dict[str, int]:
    if frame.empty or field not in frame:
        return {}
    return {str(key): int(value) for key, value in frame[field].value_counts().items()}


def eligible_mask(frame):
    return frame["eligible"].fillna(False).eq(True)


def known_flags(frame):
    entities = frame.get("related_content", pd.Series("", index=frame.index)).fillna("").astype(str)
    titles = frame.get("issue_title", pd.Series("", index=frame.index)).fillna("").astype(str)
    descriptions = frame.get("description", pd.Series("", index=frame.index)).fillna("").astype(str)
    urls = frame.get("source_url", pd.Series("", index=frame.index)).fillna("").astype(str)
    text = entities + " " + titles + " " + descriptions
    return {
        "명시적인 게임명 포함": int(text.str.contains(r"메이플스토리|로스트아크", regex=True).sum()),
        "회차·일반명 작품 식별": int(entities.str.fullmatch(
            r"(?:제?\d+\s*(?:화|회)\s*(?:예고|선공개)|메이킹|명작맛집|작품|인터뷰[①-⑳\d]*|극장가|영화|신규|상승|공개|\d+위|🔔?EP\.?\s*\d+\s*FULL)",
            case=False,
        ).sum()),
        "unsupportedbrowser 출처 링크": int(urls.str.contains("/unsupportedbrowser", regex=False).sum()),
    }


def export_csv(frame, path):
    export = frame.copy()
    for column in export.columns:
        export[column] = export[column].map(
            lambda value: json.dumps(value, ensure_ascii=False, default=str)
            if isinstance(value, (list, tuple, dict, set)) else value
        )
    export.to_csv(path, index=False, encoding="utf-8-sig")


def evidence_links(row):
    records = row.get("evidence_records", [])
    if not isinstance(records, list):
        records = []
    links = []
    for evidence in records:
        if not isinstance(evidence, dict):
            continue
        url = plain(evidence.get("url") or evidence.get("source_url"))
        if url.startswith(("https://", "http://")) and url not in [item[1] for item in links]:
            links.append((plain(evidence.get("source")) or "출처", url))
    if not links:
        url = plain(row.get("source_url"))
        if url.startswith(("https://", "http://")):
            links.append((plain(row.get("source")) or "출처", url))
    return MarkdownCell(" · ".join(
        f"[{cell(name, 28)}]({url.replace(' ', '%20').replace('|', '%7C').replace('(', '%28').replace(')', '%29')})"
        for name, url in links[:3]
    ) or "없음")


def compare(args):
    sys.path.insert(0, str(ROOT))
    from issue_quality import assess_issues, build_ranked_issues

    protected_before = {name: sha256((ROOT / name).read_bytes()) for name in DATA_FILES}
    baseline_app = baseline_file("app.py", args)
    baseline_feed = baseline_file("issue_feed.csv", args)
    feed = pd.read_csv(BytesIO(baseline_feed), sep="|", dtype=str).fillna("")
    feed["_audit_row_id"] = range(len(feed))
    recent, before_prepared, before_ranked, start, end = original_ranking(baseline_app.decode(), feed, args.as_of)
    if args.as_of == AS_OF:
        assert len(feed) == 559 and len(recent) == 104, "The fixed baseline data changed."
    current_feed_bytes = args.input.read_bytes()
    current_feed = pd.read_csv(BytesIO(current_feed_bytes), sep="|", dtype=str).fillna("")
    # Metadata migration must preserve the original records and their order.
    # Matching on title alone is unsafe: the frozen file has repeated article titles.
    assert len(current_feed) == len(feed), "Migration changed the number of original issue records."
    for field in ("date", "issue_title"):
        assert current_feed[field].tolist() == feed[field].tolist(), (
            f"Migration changed or reordered {field}; supply an explicit row-ID mapping before auditing."
        )
    current_feed["_audit_row_id"] = feed["_audit_row_id"].to_numpy()
    assessed = assess_issues(current_feed.copy(), now=args.as_of)
    required = {"_audit_row_id", "eligible", "exclusion_reason", "source_group", "related_content", "source_url"}
    missing = required - set(assessed.columns)
    if missing:
        raise AssertionError(f"Assessment lost required audit columns: {sorted(missing)}")
    assert len(assessed) == len(feed), "Assessment must preserve every input row, including exclusions."
    assert set(assessed["_audit_row_id"]) == set(feed["_audit_row_id"]), "Assessment changed original row identity."
    after_ranked = build_ranked_issues(assessed.copy())
    recent_ids = set(recent["_audit_row_id"])
    after_recent = assessed[assessed["_audit_row_id"].isin(recent_ids)].copy()
    kept = after_recent[eligible_mask(after_recent)]
    excluded = after_recent[~eligible_mask(after_recent)]
    excluded_all = assessed[~eligible_mask(assessed)]
    new_eligible = assessed[eligible_mask(assessed)]
    before_lookup = before_prepared.set_index("_audit_row_id")
    originals = feed.set_index("_audit_row_id")

    correction_fields = ("source_group", "related_content", "source_url", "date", "reference_date", "metric_date")
    correction_counts = Counter()
    decision_rows = []
    corrections = []
    for _, row in after_recent.iterrows():
        row_id = row["_audit_row_id"]
        original = originals.loc[row_id]
        previous = before_lookup.loc[row_id]
        changes = []
        output = {"audit_row_id": int(row_id), "original_date": original.get("date", ""), "original_source": original.get("source", ""), "original_title": original.get("issue_title", ""), "original_related_content": original.get("related_content", ""), "original_source_group": previous.get("source_group", ""), "original_source_url": original.get("source_url", "")}
        for field in correction_fields:
            old = plain(previous.get(field) if field == "source_group" else original.get(field))
            new = plain(row.get(field))
            if old != new:
                changes.append(f"{field}: {old} → {new}")
                correction_counts[field] += 1
        output.update({key: row.get(key, "") for key in ["eligible", "exclusion_reason", "source_group", "related_content", "source_url", "date", "reference_label", "strength_label", "evidence_status", "metric_summary", "selection_reason"]})
        output["corrections"] = "; ".join(changes)
        decision_rows.append(output)
        if changes:
            corrections.append((row_id, original.get("issue_title", ""), "; ".join(changes)))
    decisions = pd.DataFrame(decision_rows)
    reason_counts = Counter()
    for value in excluded["exclusion_reason"]:
        reason_counts.update(part.strip() for part in re.split(r";\s*", plain(value)) if part.strip())
    original_sources = originals["source"]
    kept_original_sources = kept["_audit_row_id"].map(original_sources).value_counts().to_dict()
    excluded_original_sources = excluded["_audit_row_id"].map(original_sources).value_counts().to_dict()
    source_names = sorted(source_counts(recent))
    source_rows = [(name, source_counts(recent).get(name, 0), kept_original_sources.get(name, 0), excluded_original_sources.get(name, 0)) for name in source_names]
    before_top = before_ranked.head(20)
    after_top = after_ranked.head(20)
    before_flags, after_flags = known_flags(before_top), known_flags(after_top)
    recent_original = recent.set_index("_audit_row_id")
    assessed_by_id = assessed.set_index("_audit_row_id")
    old_netflix = recent_original[recent_original["source"].eq("Netflix Top 10")]
    stale_netflix_ids = old_netflix[
        old_netflix["description"].str.contains("집계 주간: 2026-10-04", regex=False)
    ].index
    game_ids = recent_original[
        (recent_original["issue_title"] + " " + recent_original["related_content"])
        .str.contains("메이플스토리|로스트아크", regex=True)
    ].index
    unrelated_news_ids = recent_original[
        recent_original["issue_title"].str.contains("중국 사막화 방지 활동가", regex=False)
    ].index
    netflix_after = assessed_by_id.loc[old_netflix.index]
    regression_checks = {
        "원본행 식별자와 행수 보존": len(assessed) == len(feed),
        "기존 오래된 Netflix 10월4일 집계 자동선정 제외": not eligible_mask(assessed_by_id.loc[stale_netflix_ids]).any(),
        "기존 명시적 게임 영상 자동선정 제외": not eligible_mask(assessed_by_id.loc[game_ids]).any(),
        "기존 환경활동가 기사 자동선정 제외": not eligible_mask(assessed_by_id.loc[unrelated_news_ids]).any(),
        "기존 Netflix source_group OTT로 정정": netflix_after["source_group"].eq("OTT 랭킹·신작").all(),
        "기존 Netflix unsupportedbrowser 링크 제거": not netflix_after["source_url"].str.contains("/unsupportedbrowser", regex=False).any(),
        "자동선정 작품명에 알려진 회차·일반명 없음": known_flags(new_eligible)["회차·일반명 작품 식별"] == 0,
    }
    metrics = {
        "as_of": args.as_of, "timezone": "Asia/Seoul", "baseline_ref": args.baseline_ref,
        "baseline_sha256": {"app.py": sha256(baseline_app), "issue_feed.csv": sha256(baseline_feed)},
        "current_input_path": str(args.input), "current_input_sha256": sha256(current_feed_bytes),
        "current_quality_module_sha256": sha256((ROOT / "issue_quality.py").read_bytes()),
        "baseline_rows": len(feed), "baseline_recent_window": {"start": start, "end": end},
        "baseline_recent_rows": len(recent), "baseline_recent_eligible_after": len(kept),
        "baseline_recent_excluded_after": len(excluded), "all_rows_eligible_after": len(new_eligible),
        "all_rows_excluded_after": len(excluded_all),
        "eligible_outside_original_7day_rows": int((~new_eligible["_audit_row_id"].isin(recent_ids)).sum()),
        "source_counts_before_recent": source_counts(recent), "source_counts_kept_recent": source_counts(kept),
        "exclusion_reason_counts_recent": dict(reason_counts), "correction_counts_recent": dict(correction_counts),
        "ranked_groups_before": len(before_ranked), "ranked_groups_after": len(after_ranked),
        "top20_source_counts_before": source_counts(before_top), "top20_source_counts_after": source_counts(after_top),
        "top20_route_counts_before": source_counts(before_top, "source_group"), "top20_route_counts_after": source_counts(after_top, "source_group"),
        "known_rule_flags_before_top20": before_flags, "known_rule_flags_after_top20": after_flags,
        "regression_checks": {name: bool(result) for name, result in regression_checks.items()},
        "live_source_verification": False,
    }
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    export_csv(before_top, output / "before_top20_reproduced.csv")
    export_csv(assessed, output / "after_assessed_issues.csv")
    export_csv(decisions, output / "after_recent_decisions.csv")
    export_csv(after_top, output / "after_top20.csv")
    (output / "after_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    top_before_rows = [(number, row.get("related_content"), row.get("source"), row.get("issue_score"), row.get("date")) for number, (_, row) in enumerate(before_top.iterrows(), 1)]
    top_after_rows = [(number, row.get("related_content"), row.get("confirmed_routes", row.get("source_group")), row.get("reference_label"), row.get("metric_summary"), row.get("strength_label"), row.get("evidence_status"), row.get("selection_reason"), evidence_links(row)) for number, (_, row) in enumerate(after_top.iterrows(), 1)]
    exception_rows = [(int(row["_audit_row_id"]), row.get("issue_title"), row.get("exclusion_reason")) for _, row in excluded.iterrows()]
    flags = [(name, before_flags[name], after_flags[name]) for name in before_flags]
    report = f"""# 이슈 품질 수정 전후 검증

기준일: **{args.as_of} (Asia/Seoul)**. 원본 기준 코드: `{args.baseline_ref}`.

이 보고서는 원래 저장한 같은 이슈 기록을 메타데이터 보완 전후로 비교하고, 날짜를 고정한 **오프라인 회귀 비교**입니다. 현재 외부 사이트의 순위·기사 진위, 전체 시장 대비 누락률, 실제 기획 활용 성과를 측정한 보고서는 아닙니다. 원문 링크는 보존·정정된 근거 경로이며 실시간 열람 성공을 뜻하지 않습니다.

## 범위와 비교 단위

- 동결 원본 {len(feed)}행 중 기존 화면의 최근 7일({start}~{end}) 표본은 {len(recent)}행입니다. 수정 후 판정 입력은 메타데이터를 보완한 실제 `{args.input}`이며 원래 날짜·제목·행 순서가 보존됐는지 확인했습니다.
- 기존 {len(recent)}행에서 새 품질 기준을 통과한 행은 **{len(kept)}행**, 제외·보류된 행은 **{len(excluded)}행**입니다. 제외는 원본 삭제가 아니라 자동 선정 대상에서 분리하는 뜻입니다.
- 새 모듈은 전체 원본 {len(feed)}행을 판정합니다. 출처별 기준 시점·허용 기간을 적용하므로 기존 날짜 필터 밖에서 통과한 행도 {metrics['eligible_outside_original_7day_rows']}행 있습니다. 상위 결과는 이 실제 새 선정 풀을 사용합니다.
- 기존 이슈 그룹 {len(before_ranked)}개와 새 선정 그룹 {len(after_ranked)}개는 그룹화·적격성 규칙이 다르므로 개수 증가 자체를 품질 향상으로 해석하지 않습니다.
- 응답·통계가 누적 조회수뿐인 경우 실측 급증으로 바꾸어 주장하지 않습니다. 원문 접근 실패·기준일 복원·작품명 추정은 검증 완료와 구분해야 합니다.

## 기존 최근 {len(recent)}행의 출처별 판정

{markdown_table(['원래 source', '기존 표본 행', '자동 선정 통과', '제외·보류'], source_rows)}

통과 행이 없거나 적은 출처를 정상적으로 모두 수집했다고 해석하면 안 됩니다. 원문을 확인할 수 없어 보류한 출처는 주요 이슈 누락 여부도 별도로 확인해야 합니다. 이 표의 분모는 저장된 표본이며 해당 출처가 실제로 제공한 전체 콘텐츠 수가 아닙니다.

## 제외·보류 이유

복수 사유가 있는 행은 여러 항목에 포함됩니다. 따라서 아래 합계는 제외 행 수와 다를 수 있습니다.

{markdown_table(['이유', '해당 행 수'], reason_counts.most_common())}

## 정정된 필드

정정은 링크·분류·기준일·작품 식별의 코드상 보정이며, 외부 원문의 독립 확인을 뜻하지 않습니다.

{markdown_table(['필드', '기존 최근 표본 중 변경 행 수'], correction_counts.items())}

정정 예시:

{markdown_table(['원본 행 ID', '원래 제목', '변경 내역'], corrections[:12])}

## 알려진 실패 사례의 회귀 검사

{markdown_table(['검사', '결과'], ((name, '통과' if result else '실패') for name, result in regression_checks.items()))}

이 검사는 기존에 관측된 특정 결함의 재발 여부만 검사합니다. 외부 원문의 정확성이나 전체 콘텐츠 시장 대비 누락 여부를 대신 검증하지 않습니다.

## 상위 20건의 진단 지표

{markdown_table(['진단 항목', '수정 전 상위20', '수정 후 상위20'], flags)}

이 지표는 확인된 특정 문제의 회귀 여부를 보는 문자열 규칙입니다. 관련성 정확도·정밀도·재현율이 아닙니다. 게임명·회차명 규칙에 잡히지 않은 항목이 유용하다는 보장도 없습니다.

수정 전 상위20 원천 분포: `{json.dumps(metrics['top20_source_counts_before'], ensure_ascii=False)}`.

수정 후 상위20 원천 분포: `{json.dumps(metrics['top20_source_counts_after'], ensure_ascii=False)}`.

출처가 다양해졌다는 사실만으로 더 정확하다고 평가하지 않습니다. 각 항목의 관련 작품·신호·기준 시점·근거를 함께 봐야 합니다.

## 수정 전 상위20

{markdown_table(['순위', '관련 작품·표현', 'source', '기존 점수', '기존 날짜'], top_before_rows)}

기존 점수는 이전 알고리즘의 점수입니다. 새 결과의 신호 등급 또는 정렬 우선순위와 동일한 척도가 아닙니다.

## 수정 후 상위20와 근거

{markdown_table(['순위', '관련 작품', '확인 경로', '기준 시점', '관측 지표', '신호 표시', '근거 상태', '선정 이유', '근거 링크'], top_after_rows)}

## 기존 최근 표본의 제외·보류 내역

{markdown_table(['원본 행 ID', '원래 제목', '제외·보류 사유'], exception_rows)}

전체 {len(recent)}행의 통과/제외/정정 내역은 `{output / 'after_recent_decisions.csv'}`에 저장했습니다. 새 상위20 원자료는 `{output / 'after_top20.csv'}`, 전체 평가 결과는 `{output / 'after_assessed_issues.csv'}`입니다.

## 아직 확인하지 못한 것

- Netflix·KOBIS 사이트와 Google News 중계 URL은 이 환경에서 프록시 CONNECT 403으로 차단된 바 있습니다. 이번 스크립트는 네트워크 요청을 하지 않으므로 새 실시간 접근 검증 결과가 없습니다.
- 현재 저장본으로부터 복원한 업로드일·집계일·매체·작품 정보는 원본 API 식별자나 원문 본문을 새로 확인한 결과와 다릅니다.
- 원문에 없는 작품명·급증량·공식 발행자 정보를 보충해서 사실로 만들면 안 됩니다. 불확실한 연결은 검토 대상으로 남겨야 합니다.
- 실제 편성 가능성은 B tv+ 보유·계약·노출 가능 콘텐츠와 별도 대조가 필요합니다. 이 저장소의 이슈 CSV만으로 보장할 수 없습니다.
- 품질 성과를 평가하려면 원문 접근이 가능한 환경에서 상위20~30개를 편집자가 관련성·작품 식별·최신성·근거 기준으로 별도 판정하고, 1~2주간 반복 관측해야 합니다.

## 재현 명령

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/audit_issue_quality.py --as-of {args.as_of} --baseline-ref {args.baseline_ref}
```

기본 산출물 경로는 시스템 임시 디렉터리이며 `--output-dir`로 변경할 수 있습니다. 원본 코드·CSV를 Git 기준 커밋에서 읽습니다. 해당 커밋이 없는 얕은 체크아웃은 먼저 커밋을 확보하거나 `--baseline-dir`로 보존한 `before_*` 파일 경로를 지정해야 합니다.

동결 원본 SHA256: `{sha256(baseline_feed)}`. 실제 수정 후 CSV SHA256: `{sha256(current_feed_bytes)}`. 새 품질 모듈 SHA256: `{metrics['current_quality_module_sha256']}`.
"""
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    protected_after = {name: sha256((ROOT / name).read_bytes()) for name in DATA_FILES}
    assert protected_after == protected_before, "Audit unexpectedly modified application CSV data."
    print(json.dumps(metrics, ensure_ascii=False, indent=2, default=str))
    print(f"Report: {args.report}")
    print("Application CSV SHA256 unchanged. No collector, AI call, or live source verification ran.")
    failed = [name for name, result in regression_checks.items() if not result]
    if failed:
        raise AssertionError("Known issue-quality regressions remain: " + "; ".join(failed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=AS_OF)
    parser.add_argument("--baseline-ref", default=BASELINE_REF)
    parser.add_argument("--baseline-dir", type=Path)
    parser.add_argument("--input", type=Path, default=ROOT / "issue_feed.csv")
    parser.add_argument("--output-dir", type=Path, default=Path(tempfile.gettempdir()) / "btv-issue-audit")
    parser.add_argument("--report", type=Path, default=ROOT / "artifacts" / "issue_quality_audit.md")
    args = parser.parse_args()
    date.fromisoformat(args.as_of)
    compare(args)


if __name__ == "__main__":
    main()
