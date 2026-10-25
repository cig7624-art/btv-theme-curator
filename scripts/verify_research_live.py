"""Verify two real research turns and encrypted recovery through the public UI.

Each paid question is attempted once. A timeout never resubmits it. The report
contains fixed checks, sanitized counters, versions, and public source hosts;
never prompts, model prose, recovery codes, HTML, browser state, or screenshots.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_deployment import (  # noqa: E402
    APP_URL, VerificationIssue, _load_version, has_app_exception,
    no_horizontal_overflow, normalized_version,
)


BRIEF = "운영 검증용 사업 조건: 한국의 B tv+ 단품 기획. 목표 고객은 가족 고객. 원가와 예산은 미제공."
QUESTIONS = (
    "B tv+ 단품 가격을 검토하려고 합니다. 국내외 공식 자료에서 확인한 요금제나 상품 사례를 "
    "근거와 함께 비교하고, 사업 조건을 반영한 검토안과 아직 필요한 정보를 제안해 주세요. "
    "원가를 모르는 상태에서 최적 가격을 확정하지 마세요.",
    "앞서 채택한 결정에서 정한 검토 기준의 이름을 먼저 언급하고, 사업 조건의 목표 고객을 반영해서 비플 브랜딩의 키메시지 방향을 "
    "검토해 주세요. 현재 메시지는 '부담은 낮추고 콘텐츠는 가득'입니다. "
    "해외 미디어 사례에서 확인한 사실과 제안을 구분하고 두 가지 키메시지 안을 제안해 주세요.",
)
DECISION = "가격 확정은 보류하고 가족 고객을 대상으로 가격 수용도부터 검증한다. 이 검토 기준의 이름은 '가족 나침반'이다."
DRAFT = "추가 검토할 질문을 아직 작성하고 있습니다"
RECOVERY_CODE = re.compile(r"rp_[A-Za-z0-9_-]{43}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}\Z")
_ENGINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,39}\Z")


def _require(condition, code):
    if not condition:
        raise VerificationIssue(code)


def _count(value, maximum=100000):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if str(number) == str(value) and 0 <= number <= maximum else None


def safe_result_metadata(value):
    """Only fixed labels and bounded numeric result metadata may be recorded."""
    if not isinstance(value, dict):
        return None
    fields = {key: _count(value.get(key)) for key in
              ("fact_count", "source_count", "option_count", "added_documents")}
    if any(number is None for number in fields.values()):
        return None
    status, search = value.get("status"), value.get("search_status")
    version = value.get("engine_version")
    model = value.get("model")
    if (status not in {"grounded", "conditional"} or search not in {"enriched", "archive_only"}
            or not isinstance(version, str) or not _ENGINE.fullmatch(version)
            or not isinstance(model, str) or not _MODEL.fullmatch(model)):
        return None
    return {**fields, "status": status, "search_status": search,
            "engine_version": version, "model": model}


def memory_answer_supports_context(text):
    """Inspect prose in memory; only its boolean result leaves the browser."""
    return bool(isinstance(text, str) and re.search(r"가족\s*나침반", text) and re.search(
        r"가격.{0,35}(?:보류|수용도|검증)|수용도.{0,25}(?:확인|조사|검증)", text, re.S))


def public_source_hosts(urls):
    result = set()
    for value in urls:
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").lower().rstrip(".")
            if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                    or host == "localhost" or host.endswith((".local", ".internal"))
                    or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", host) or "." not in host):
                continue
            try:
                if not ipaddress.ip_address(host).is_global:
                    continue
            except ValueError:
                pass
            result.add(host)
        except (ValueError, TypeError):
            continue
    return sorted(result)


def submit_question_once(composer, report, position, question):
    """Record an attempt before pressing Enter; an uncertain click is final."""
    _require(type(position) is int and position in {1, 2}, "question_index_invalid")
    key = "question_" + str(position) + "_attempted"
    _require(not report["checks"].get(key), "question_already_attempted")
    _require(report["counts"].get("questions_attempted", 0) < 2, "question_budget_exhausted")
    _require(composer.is_enabled(), "research_composer_disabled")
    composer.fill(question)
    report["checks"][key] = True
    report["counts"]["questions_attempted"] = report["counts"].get("questions_attempted", 0) + 1
    composer.press("Enter", timeout=15000)


def _alert_code(text):
    for phrase, code in (("잔액", "provider_quota_unavailable"),
                         ("한도", "provider_quota_unavailable"),
                         ("연결 키", "provider_key_unavailable"),
                         ("모델", "research_model_unavailable"),
                         ("저장", "research_storage_unavailable"),
                         ("답변", "research_answer_unavailable")):
        if phrase in text:
            return code
    return "research_action_unavailable"


def _load_research(page, args, report, label):
    release = {"tests": {}}
    try:
        frame = _load_version(page, args, release, label)
    finally:
        if release.get("observed_version"):
            report["observed_version"] = release["observed_version"]
        report["checks"][label + "_exact_version"] = bool(release["tests"].get(label + "_version"))
    frame.get_by_role("tab", name="미디어 인사이트", exact=True).click()
    frame.locator(".media-insights-heading").wait_for(state="visible", timeout=30000)
    toggle = frame.get_by_role("button", name="리서치 질문하기", exact=True)
    toggle.wait_for(state="visible", timeout=30000)
    _require(toggle.is_enabled(), "research_action_disabled")
    toggle.click()
    frame.locator(".st-key-media_research_panel").wait_for(state="visible", timeout=30000)
    composer = frame.locator(".st-key-media_research_question textarea")
    composer.wait_for(state="visible", timeout=30000)
    _require(composer.is_enabled(), "research_composer_disabled")
    report["checks"][label + "_composer"] = True
    return frame


def _state(frame):
    return frame.locator(".media-research-state").last.evaluate("""element => ({
      turns: element.dataset.turnCount, decisions: element.dataset.decisionCount,
      storage: element.dataset.storageStatus
    })""")


def _wait_state(frame, turns, decisions=None, *, timeout=30000, deadline=None, storage=None):
    end = min(deadline or float("inf"), time.monotonic() + timeout / 1000)
    while time.monotonic() < end:
        _require(not has_app_exception(frame.page), "app_exception")
        marker = frame.locator(".media-research-state")
        if marker.count():
            state = _state(frame)
            if (_count(state.get("turns")) == turns
                    and (decisions is None or _count(state.get("decisions")) == decisions)
                    and (storage is None or state.get("storage") == storage)):
                stale = marker.last.evaluate("element => !!element.closest('[data-stale=true]')")
                if not stale:
                    return state
        alert = frame.locator(".st-key-media_research_panel [data-testid='stAlert']")
        if alert.count():
            raise VerificationIssue(_alert_code(alert.last.inner_text()))
        frame.page.wait_for_timeout(200)
    raise VerificationIssue("research_result_not_ready")


def _metadata(frame, position):
    # The state counter streams before its answer body. Wait for that precise
    # result and the completed composer instead of sampling a preceding turn.
    frame.locator(".media-research-result").nth(position - 1).wait_for(state="attached", timeout=30000)
    frame.wait_for_function('''position => {
      const panel = document.querySelector('.st-key-media_research_panel');
      const composer = panel?.querySelector('.st-key-media_research_question textarea');
      return panel && composer && !composer.disabled
        && panel.querySelectorAll('.media-research-result').length >= position
        && !panel.querySelector('[data-stale=true]') && !panel.closest('[data-stale=true]');
    }''', arg=position, timeout=30000)
    raw = frame.locator(".media-research-result").nth(position - 1).evaluate("""element => ({
      fact_count: element.dataset.factCount, source_count: element.dataset.sourceCount,
      option_count: element.dataset.optionCount, status: element.dataset.status,
      engine_version: element.dataset.engineVersion, model: element.dataset.model,
      search_status: element.dataset.searchStatus, added_documents: element.dataset.addedDocuments
    })""")
    result = safe_result_metadata(raw)
    _require(result is not None, "research_result_metadata_invalid")
    return result


def _management(frame, *, expanded=True):
    expander = frame.locator(".st-key-media_research_panel [data-testid='stExpander']").filter(
        has=frame.get_by_text("사업 조건·프로젝트 관리", exact=True)).first
    summary = expander.locator("summary")
    summary.wait_for(state="visible", timeout=15000)
    is_open = expander.evaluate('''element => {
      const details = element.matches('details') ? element : element.querySelector('details');
      return details ? details.open : element.querySelector('summary')?.getAttribute('aria-expanded') === 'true';
    }''')
    if is_open != expanded:
        summary.click()
    return expander


def _recovery_code(frame):
    _management(frame)
    for code in frame.locator(".st-key-media_research_panel code").all_text_contents():
        value = code.strip()
        if RECOVERY_CODE.fullmatch(value):
            return value
    raise VerificationIssue("project_recovery_code_missing")


def _open_project(frame, code, deadline):
    _require(bool(RECOVERY_CODE.fullmatch(code)), "project_recovery_code_invalid")
    _management(frame)
    frame.locator(".st-key-media_research_open_code input").fill(code)
    frame.get_by_role("button", name="프로젝트 이어가기", exact=True).click()
    state = _wait_state(frame, 2, 1, deadline=deadline)
    _require(state.get("storage") == "remote_synced", "project_backup_not_synced")
    _management(frame, expanded=False)
    return state


def _draft_roundtrip(frame, label, report):
    composer = frame.locator(".st-key-media_research_question textarea")
    composer.fill(DRAFT)
    frame.get_by_role("tab", name="추천 테마", exact=True).click()
    frame.locator(".st-key-curation_section").wait_for(state="visible", timeout=30000)
    frame.get_by_role("tab", name="미디어 인사이트", exact=True).click()
    composer.wait_for(state="visible", timeout=30000)
    report["checks"][label + "_unsent_draft_retained"] = composer.input_value() == DRAFT
    _require(report["checks"][label + "_unsent_draft_retained"], "research_unsent_draft_lost")
    composer.fill("")


def _no_overflow(frame, label, report):
    dimensions = frame.evaluate("""() => {
      const main = document.querySelector('[data-testid="stMain"]');
      return {viewport: innerWidth, document_scroll: document.documentElement.scrollWidth,
        main_width: main ? main.clientWidth : innerWidth,
        main_scroll: main ? main.scrollWidth : document.documentElement.scrollWidth};
    }""")
    report["checks"][label + "_no_overflow"] = no_horizontal_overflow(dimensions)
    _require(report["checks"][label + "_no_overflow"], "research_horizontal_overflow")


def verify(args):
    report = {"schema_version": 1, "status": "failed", "expected_version": args.expected_version,
              "observed_version": None, "started_at": datetime.now(timezone.utc).isoformat(),
              "finished_at": None, "checks": {}, "counts": {"questions_attempted": 0},
              "responses": [], "source_hosts": [], "errors": []}
    deadline = time.monotonic() + args.timeout
    source_sha = os.environ.get("GITHUB_SHA", "")
    if re.fullmatch(r"[a-f0-9]{40}", source_sha):
        report["source_commit"] = source_sha
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            launch = {"headless": True, "args": ["--disable-dev-shm-usage"]}
            if args.executable:
                launch["executable_path"] = args.executable
            browser = playwright.chromium.launch(**launch)
            try:
                desktop = browser.new_context(viewport={"width": 1366, "height": 1000})
                try:
                    page = desktop.new_page()
                    frame = _load_research(page, args, report, "desktop")
                    _draft_roundtrip(frame, "desktop", report)
                    _management(frame)
                    frame.locator(".st-key-media_research_project_title input").fill("운영 검증용 사업 리서치")
                    frame.locator(".st-key-media_research_brief_editor textarea").fill(BRIEF)
                    frame.get_by_role("button", name="사업 조건 저장", exact=True).click()
                    state = _wait_state(frame, 0, 0, deadline=deadline, storage="remote_synced")
                    _require(state.get("storage") == "remote_synced", "project_backup_not_synced")
                    code = _recovery_code(frame)  # Never record or print this capability.
                    _management(frame, expanded=False)
                    for position, question in enumerate(QUESTIONS, 1):
                        started = time.monotonic()
                        submit_question_once(frame.locator(".st-key-media_research_question textarea"),
                                             report, position, question)
                        state = _wait_state(frame, position, timeout=args.response_timeout * 1000,
                                            deadline=deadline)
                        _require(state.get("storage") == "remote_synced", "project_backup_not_synced")
                        metadata = _metadata(frame, position)
                        metadata["completion_seconds"] = round(time.monotonic() - started, 2)
                        report["responses"].append(metadata)
                        _require(metadata["option_count"] > 0, "research_business_options_missing")
                        if position == 1:
                            _require(metadata["fact_count"] > 0 and metadata["source_count"] > 0,
                                     "research_verified_facts_missing")
                            report["checks"]["pricing_facts_and_sources"] = True
                            adoption = frame.locator(".st-key-media_research_panel [data-testid='stExpander']").filter(
                                has=frame.get_by_text("이 제안을 결정으로 저장", exact=True)).last
                            adoption.locator("summary").click()
                            adoption.get_by_label("기억할 결론", exact=True).fill(DECISION)
                            adoption.get_by_role("button", name="이 결론 채택", exact=True).click()
                            _wait_state(frame, 1, 1, deadline=deadline)
                            report["checks"]["decision_explicitly_adopted"] = True
                        else:
                            # This text remains only in memory. The artifact gets
                            # one boolean, never a model answer or business detail.
                            answer_text = frame.locator(".st-key-media_research_panel [data-testid='stChatMessage']").last.inner_text()
                            report["checks"]["followup_uses_brief_and_decision"] = memory_answer_supports_context(answer_text)
                            _require(report["checks"]["followup_uses_brief_and_decision"], "research_followup_context_missing")
                    report["source_hosts"] = public_source_hosts(
                        frame.locator(".st-key-media_research_panel [data-testid='stLinkButton'] a[href]").evaluate_all(
                            "elements => elements.map(element => element.href)"))
                    _require(bool(report["source_hosts"]), "research_source_links_missing")
                    _no_overflow(frame, "desktop", report)
                    report["checks"]["desktop_no_exception"] = not has_app_exception(page)
                    _require(report["checks"]["desktop_no_exception"], "app_exception")
                finally:
                    desktop.close()
                # A fresh session has no access to the previous session state.
                # Reopening by the in-memory code proves encrypted persistence.
                for label, width, height in (("reopened_desktop", 1366, 1000), ("mobile", 390, 844)):
                    context = browser.new_context(viewport={"width": width, "height": height},
                                                  is_mobile=label == "mobile", has_touch=label == "mobile")
                    try:
                        page = context.new_page()
                        frame = _load_research(page, args, report, label)
                        _open_project(frame, code, deadline)
                        report["checks"][label + "_recovered_two_turns_and_decision"] = True
                        if label == "mobile":
                            _draft_roundtrip(frame, label, report)
                        _no_overflow(frame, label, report)
                        report["checks"][label + "_no_exception"] = not has_app_exception(page)
                        _require(report["checks"][label + "_no_exception"], "app_exception")
                    finally:
                        context.close()
                report["counts"]["stored_turns"] = 2
                report["counts"]["stored_decisions"] = 1
                report["checks"]["exactly_two_once_only_questions"] = report["counts"]["questions_attempted"] == 2
                report["status"] = "passed"
            finally:
                browser.close()
    except VerificationIssue as error:
        report["status"] = "blocked" if error.blocked else "failed"
        report["errors"] = [error.code]
    except Exception as error:
        blocked = any(code in str(error) for code in ("ERR_TUNNEL_CONNECTION_FAILED", "ERR_PROXY_CONNECTION_FAILED",
                                                     "ERR_ACCESS_DENIED", "ERR_CERT_AUTHORITY_INVALID"))
        report["status"] = "blocked" if blocked else "failed"
        report["errors"] = ["network_access_blocked" if blocked else "research_browser_check_unavailable"]
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=APP_URL)
    parser.add_argument("--expected-version")
    parser.add_argument("--timeout", type=float, default=480)
    parser.add_argument("--response-timeout", type=float, default=180)
    parser.add_argument("--executable", default=os.environ.get("RESEARCH_CHROMIUM_PATH"))
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/research_live_validation.json")
    args = parser.parse_args(argv)
    args.expected_version = normalized_version(args.expected_version or (ROOT / "VERSION").read_text().strip())
    url = urlsplit(args.url)
    if (not args.expected_version or not 0 < args.timeout <= 480 or not 0 < args.response_timeout <= 180
            or url.scheme != "https" or not url.hostname or url.username or url.password):
        parser.error("Use an exact release version, public HTTPS URL, and bounded timeouts")
    report = verify(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n")
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if report["status"] == "passed" else 2 if report["status"] == "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
