"""Verify up to three genuine media analyses and one research answer through the live UI.

Paid buttons are attempted once each, only after the exact deployed VERSION is
confirmed. A timeout after a click is uncertain and never causes a resubmission.
Artifacts contain only release versions, counts and booleans. Prompts, answers,
HTML, browser storage, credentials and provider errors are never recorded.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_deployment import (  # noqa: E402
    APP_URL, VerificationIssue, _load_version, has_app_exception,
    no_horizontal_overflow, normalized_version,
)

SNAPSHOT_URL = 'https://raw.githubusercontent.com/cig7624-art/btv-theme-curator/main/media_insights_snapshot.json'
QUESTION = ('이 공식 발표에서 확인되는 핵심 변화 한 가지를 원문 근거와 함께 설명하고, '
            '이 발표만으로 B tv의 예상 매출을 확인할 수 있는지도 구분해 주세요.')


def _require(condition, code):
    if not condition:
        raise VerificationIssue(code)


def _public_url(value):
    try:
        parsed = urlsplit(value)
        return bool(parsed.scheme == 'https' and parsed.hostname and not parsed.username
                    and not parsed.password and parsed.hostname not in {'localhost', '127.0.0.1'})
    except ValueError:
        return False


def answer_distinguishes_unknown(value):
    """A cited factual answer must also acknowledge the requested internal unknown."""
    return bool(re.search(r'B\s*tv', value, re.I) and re.search(
        r'확인.{0,12}(?:없|어렵|못)|(?:공개|자료|근거|정보).{0,12}(?:없|부족)|'
        r'(?:추정|산출|계산).{0,12}(?:불가|어렵|없)|알\s*수\s*없|'
        r'(?:제공|제시|포함|공개).{0,12}(?:않|없)|미공개|확정.{0,12}(?:없|어렵)', value))


def saved_note_valid(snapshot, source_url):
    """Verify public persistence without exporting any document or model prose."""
    if not isinstance(snapshot, dict):
        return False
    documents = {row.get('id'): row for row in snapshot.get('items', []) if isinstance(row, dict)}
    evidence = {}
    for document in documents.values():
        for row in document.get('evidence', []):
            if isinstance(row, dict) and row.get('id'):
                key = row['id']
                evidence[key] = None if key in evidence else document
    for document in documents.values():
        if document.get('url') != source_url:
            continue
        note = document.get('analysis')
        if (not isinstance(note, dict) or note.get('source_hash') != document.get('content_hash')
                or not note.get('generated_at') or not note.get('model') or not note.get('facts')):
            continue
        hashes = note.get('context_hashes', {})
        if not isinstance(hashes, dict) or any(documents.get(key, {}).get('content_hash') != value
                                             for key, value in hashes.items()):
            continue
        valid = True
        for statement in [*note.get('facts', []), *note.get('comparison', [])]:
            refs = statement.get('evidence_ids', []) if isinstance(statement, dict) else []
            if not refs or any(not isinstance(ref, str) or not evidence.get(ref) for ref in refs):
                valid = False
                break
            if statement in note['facts'] and any(evidence[ref]['id'] != document['id'] for ref in refs):
                valid = False
                break
            if any(evidence[ref]['id'] != document['id'] and
                   hashes.get(evidence[ref]['id']) != evidence[ref].get('content_hash') for ref in refs):
                valid = False
                break
        if valid:
            return True
    return False


def _public_saved_note(source_url):
    # Public, fixed GitHub endpoint only; no token, cookies or response logging.
    try:
        request = Request(SNAPSHOT_URL + '?live_check=' + str(int(time.time())),
                          headers={'Accept': 'application/json', 'Cache-Control': 'no-cache'})
        with urlopen(request, timeout=20) as response:
            payload = response.read(5_000_001)
        return len(payload) <= 5_000_000 and saved_note_valid(json.loads(payload), source_url)
    except Exception:
        return False


def _load_media(page, args, report, label):
    # The helper uses its own small report so no arbitrary state strings escape.
    release = {'tests': {}}
    try:
        frame = _load_version(page, args, release, label)
    finally:
        if release.get('observed_version'):
            report['observed_version'] = release['observed_version']
        report['checks'][label + '_exact_version'] = bool(release['tests'].get(label + '_version'))
    frame.get_by_role('tab', name='미디어 인사이트', exact=True).click()
    frame.locator('.media-insights-heading').wait_for(state='visible', timeout=30000)
    frame.locator('.media-story-meta').first.wait_for(state='visible', timeout=30000)
    frame.locator('.st-key-media_insights_analyze button').wait_for(state='visible', timeout=30000)
    _require(not has_app_exception(page), 'app_exception')
    return frame


def _wait_ready(page, args, report):
    deadline = time.monotonic() + args.deploy_timeout
    while True:
        report['counts']['release_load_attempts'] += 1
        try:
            return _load_media(page, args, report, 'desktop')
        except VerificationIssue as error:
            if error.blocked or not error.retry or time.monotonic() >= deadline:
                raise
        except Exception:
            if time.monotonic() >= deadline:
                raise VerificationIssue('release_not_ready')
        # All reloads are confined to this unpaid preflight phase.
        page.wait_for_timeout(15000)


def _choose_unanalysed(frame, excluded_selectors=()):
    cards = frame.locator('[class*="st-key-media_story_"]')
    for position in range(min(cards.count(), 12)):
        card = cards.nth(position)
        classes = card.get_attribute('class') or ''
        match = re.search(r'\bst-key-media_story_[a-f0-9]{16}\b', classes)
        _require(bool(match), 'selected_document_marker_missing')
        selector = '.' + match.group(0)
        if selector in excluded_selectors:
            continue
        card.get_by_role('button').click()
        card.locator('.media-story-selected').wait_for(state='visible', timeout=30000)
        frame.wait_for_function("() => !document.querySelector('[data-stale=true]')", timeout=30000)
        button = frame.locator('.st-key-media_insights_analyze button')
        button.wait_for(state='visible', timeout=30000)
        if button.inner_text().strip() == '분석 노트 만들기' and button.is_enabled():
            source = frame.get_by_role('link', name='공식 원문 열기 ↗', exact=True).get_attribute('href')
            _require(_public_url(source), 'official_link_missing')
            return selector, source
    raise VerificationIssue('unanalysed_document_unavailable')


def submit_once(button, report, action, *, action_index=None):
    """Mark an attempt before clicking; an ambiguous browser error is not retryable."""
    _require(action_index is None or (isinstance(action_index, int) and 1 <= action_index <= 3),
             'paid_action_index_invalid')
    unique_action = action if action_index is None else action + '_' + str(action_index)
    key = unique_action + '_click_attempted'
    _require(not report['checks'].get(key), 'paid_action_already_attempted')
    _require(button.is_enabled(), 'paid_action_disabled')
    report['checks'][key] = True
    report['counts'][action + '_clicks'] += 1
    if unique_action != action:
        report['counts'][unique_action + '_clicks'] = report['counts'].get(unique_action + '_clicks', 0) + 1
    button.click(timeout=15000)


def media_alert_code(value):
    """Only fixed categories leave the browser, never arbitrary alert text."""
    for phrase, code in (
        ("분석의 수치를", "media_numbers_unverified"),
        ("비교 대상 사업자를", "media_company_unverified"),
        ("사업 범위를", "media_scope_unverified"),
        ("지표의 정의를", "media_metric_unverified"),
        ("잔액", "provider_insufficient_quota"),
        ("AI 연결 키", "provider_invalid_api_key"),
        ("API 키", "provider_invalid_api_key"),
        ("AI 모델을 사용할 수", "provider_model_unavailable"),
        ("모델 설정", "provider_model_unavailable"),
        ("분석 요청이 맞지", "provider_unsupported_parameter"),
        ("요청 설정", "provider_unsupported_parameter"),
        ("분석 연결에 실패", "media_provider_failure"),
        ("GitHub", "media_github_save_failed"),
        ("저장", "media_save_failed"),
        ("원문 근거를", "media_citations_unverified"),
        ("분석할 사실을", "media_no_verified_facts"),
    ):
        if phrase in value:
            return code
    return "media_action_failed"


def _wait_result(frame, selector, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _require(not has_app_exception(frame.page), 'app_exception')
        alert = frame.locator('.st-key-media_insights_section [data-testid="stAlert"]')
        if alert.count():
            raise VerificationIssue(media_alert_code(alert.first.inner_text()))
        result = frame.locator(selector)
        if result.count() and not frame.locator('[data-stale=true]').count():
            return result
        frame.page.wait_for_timeout(500)
    raise VerificationIssue('media_action_uncertain_timeout')


def _no_overflow(frame):
    return no_horizontal_overflow(frame.evaluate('''() => {
      const main = document.querySelector('[data-testid="stMain"]');
      return {viewport:innerWidth, document_scroll:document.documentElement.scrollWidth,
              main_width:main ? main.clientWidth : innerWidth,
              main_scroll:main ? main.scrollWidth : document.documentElement.scrollWidth};
    }'''))


def exercise(page, args, report, browser):
    frame = _wait_ready(page, args, report)
    report['counts']['visible_documents'] = frame.locator('.media-story-meta').count()
    selected_documents = []
    for position in range(1, args.analysis_limit + 1):
        selector, source_url = _choose_unanalysed(frame, {row[0] for row in selected_documents})
        _require(source_url not in {row[1] for row in selected_documents}, 'duplicate_document_selected')
        _require(frame.locator('.media-note-section').count() == 0, 'existing_analysis_selected')
        prefix = 'analysis_' + str(position)
        report['checks'][prefix + '_unanalysed_official_document_selected'] = True
        submit_once(frame.locator('.st-key-media_insights_analyze button'), report, 'analysis', action_index=position)
        _wait_result(frame, '.media-note-section', args.turn_timeout)
        report['counts'][prefix + '_sections'] = frame.locator('.media-note-section').count()
        report['counts'][prefix + '_confirmed_facts'] = frame.locator('.media-note-section').filter(
            has_text='확인한 사실').locator('li').count()
        report['checks'][prefix + '_has_confirmed_facts'] = report['counts'][prefix + '_confirmed_facts'] > 0
        _require(report['checks'][prefix + '_has_confirmed_facts'], 'confirmed_facts_missing')
        report['checks'][prefix + '_has_original_evidence'] = frame.locator('.media-evidence-row').count() > 0
        _require(report['checks'][prefix + '_has_original_evidence'], 'analysis_citations_missing')
        selected_documents.append((selector, source_url))
    report['counts']['unique_analysed_documents'] = len(selected_documents)
    report['checks']['requested_analysis_count_completed'] = len(selected_documents) == args.analysis_limit
    frame.get_by_role('button', name='리서치 질문하기', exact=True).click()
    panel = frame.locator('.st-key-media_research_panel')
    field = panel.get_by_role('textbox', name='조사할 질문', exact=True)
    field.wait_for(state='visible', timeout=30000)
    field.fill(QUESTION)
    submit_once(panel.get_by_role('button', name='근거 찾아 답하기', exact=True), report, 'question')
    answers = _wait_result(frame, '.media-research-answer', args.turn_timeout)
    report['counts']['research_answers'] = answers.count()
    answer_text = answers.first.locator('p').inner_text()
    report['checks']['answer_distinguishes_unknown'] = answer_distinguishes_unknown(answer_text)
    links = frame.locator('.media-answer-sources a')
    report['counts']['research_source_links'] = links.count()
    report['checks']['answer_has_source_links'] = links.count() > 0 and all(
        _public_url(url) for url in links.evaluate_all('nodes => nodes.map(node => node.href)'))
    _require(report['checks']['answer_has_source_links'], 'research_citations_missing')
    _require(report['checks']['answer_distinguishes_unknown'], 'research_unknown_not_distinguished')
    report['checks']['desktop_no_overflow'] = _no_overflow(frame)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.wait_for_timeout(500)
    report['checks']['mobile_no_overflow'] = _no_overflow(frame)
    report['checks']['mobile_note_and_answer_visible'] = (
        frame.locator('.media-note-section').count() > 0 and frame.locator('.media-research-answer').count() == 1)
    # New anonymous browser session proves persistence independent of UI cache.
    persisted_context = browser.new_context(viewport={'width': 1366, 'height': 1000})
    try:
        saved_page = persisted_context.new_page()
        saved_frame = _load_media(saved_page, args, report, 'reload')
        for position, (selector, source_url) in enumerate(selected_documents, 1):
            saved_frame.locator(selector).get_by_role('button').click()
            saved_frame.locator(selector + ' .media-story-selected').wait_for(state='visible', timeout=30000)
            saved_frame.wait_for_function("""url => {
              const source = Array.from(document.querySelectorAll('a')).find(node => node.textContent.trim() === '공식 원문 열기 ↗');
              return source && source.href === url && !document.querySelector('[data-stale=true]');
            }""", arg=source_url, timeout=30000)
            _wait_result(saved_frame, '.media-note-section', min(45, args.turn_timeout))
            key = 'analysis_' + str(position) + '_persisted_after_reload'
            report['checks'][key] = (
                saved_frame.get_by_role('link', name='공식 원문 열기 ↗', exact=True).get_attribute('href') == source_url)
            _require(report['checks'][key], 'saved_analysis_missing')
            report['checks']['analysis_' + str(position) + '_public_github_verified'] = _public_saved_note(source_url)
    finally:
        persisted_context.close()
    report['checks']['no_app_exception'] = not has_app_exception(page)
    _require(all(report['checks'][key] for key in (
        'desktop_no_overflow', 'mobile_no_overflow', 'mobile_note_and_answer_visible', 'no_app_exception')),
        'responsive_or_app_check_failed')


def verify(args):
    report = {'schema_version': 1, 'expected_version': args.expected_version, 'observed_version': None,
              'passed': False, 'blocked': False, 'checks': {},
              'counts': {'release_load_attempts': 0, 'analysis_clicks': 0, 'question_clicks': 0,
                         'requested_analyses': args.analysis_limit}}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            launch = {'headless': True, 'args': ['--disable-dev-shm-usage']}
            if args.executable:
                launch['executable_path'] = args.executable
            browser = playwright.chromium.launch(**launch)
            try:
                context = browser.new_context(viewport={'width': 1366, 'height': 1000})
                try:
                    exercise(context.new_page(), args, report, browser)
                    report['passed'] = True
                finally:
                    context.close()
            finally:
                browser.close()
    except VerificationIssue as error:
        report['blocked'] = bool(error.blocked)
        report['failure_code'] = error.code
        report['checks']['verification_completed'] = False
    except Exception:
        report['failure_code'] = 'browser_verification_failed'
        report['checks']['verification_completed'] = False
    else:
        report['checks']['verification_completed'] = True
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=APP_URL)
    parser.add_argument('--expected-version')
    parser.add_argument('--analysis-limit', type=int, choices=range(1, 4), default=1,
                        help='Analyse 1 to 3 distinct documents once each; default 1')
    parser.add_argument('--deploy-timeout', type=float, default=480)
    parser.add_argument('--turn-timeout', type=float, default=240)
    parser.add_argument('--executable', default=os.environ.get('MEDIA_INSIGHTS_CHROMIUM_PATH'))
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/media_insights_live_validation.json')
    args = parser.parse_args(argv)
    args.expected_version = normalized_version(args.expected_version or (ROOT / 'VERSION').read_text())
    if (not args.expected_version or tuple(map(int, args.expected_version.split('-')[0].split('+')[0].split('.'))) < (1, 3, 0)
            or not _public_url(args.url) or not 0 < args.deploy_timeout <= 900 or not 0 < args.turn_timeout <= 300):
        parser.error('Require release 1.3.0 or later, a public HTTPS URL, and bounded positive timeouts')
    report = verify(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 2 if report['blocked'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
