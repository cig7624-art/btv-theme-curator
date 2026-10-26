"""Read-only check that just-collected public news reached PC and mobile."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_deployment import (
    APP_URL, VerificationIssue, _load_version, has_app_exception,
    no_horizontal_overflow, normalized_version, valid_news_digest,
)


def expected_digest(root=ROOT):
    import pandas as pd
    from issue_collection_context import apply_collection_context
    from issue_quality import assess_issues
    from news_digest import news_digest_revision, select_news_digest
    frame = pd.read_csv(root / 'issue_feed.csv', sep='|').fillna('')
    status = json.loads((root / 'collection_status.json').read_text())
    selected = select_news_digest(assess_issues(apply_collection_context(frame, status)), limit=4)
    return news_digest_revision(selected), len(selected)


def observed_matches(expected_revision, expected_count, revision, count, dates, *, now=None):
    return (revision == expected_revision and count == expected_count
            and valid_news_digest(dates, 4, count, now=now))


def verify(args):
    revision, count = expected_digest()
    source_state = json.loads((ROOT / 'collection_status.json').read_text()).get('sources', {}).get('news', {}).get('state', '')
    source_state = source_state if source_state in {'ok', 'partial', 'empty', 'failed'} else 'unknown'
    report = {'status': 'failed', 'expected_version': args.expected_version,
              'expected_revision': revision, 'expected_count': count,
              'collection_state': source_state,
              'started_at': datetime.now(timezone.utc).isoformat(), 'tests': {}, 'errors': [], 'screens': {}}
    deadline = time.monotonic() + args.timeout
    try:
        if source_state == 'failed':
            raise VerificationIssue('news_collection_failed')
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            launch = {'headless': True, 'args': ['--disable-dev-shm-usage']}
            if args.executable:
                launch['executable_path'] = args.executable
            browser = playwright.chromium.launch(**launch)
            try:
                for label, width, height in (('desktop', 1366, 1000), ('mobile', 390, 844)):
                    context = browser.new_context(viewport={'width': width, 'height': height},
                                                  is_mobile=label == 'mobile', has_touch=label == 'mobile')
                    try:
                        page = context.new_page()
                        while time.monotonic() < deadline:
                            try:
                                frame = _load_version(page, args, report, label)
                                marker = frame.locator('.news-digest[data-news-revision]')
                                marker.wait_for(state='visible', timeout=20000)
                                actual_revision = marker.get_attribute('data-news-revision')
                                actual_count = int(marker.get_attribute('data-news-count'))
                                dates = marker.locator('.news-digest-card').evaluate_all(
                                    'els=>els.map(el=>el.dataset.newsPublishedAt)')
                                if not observed_matches(revision, count, actual_revision, actual_count, dates):
                                    raise VerificationIssue('published_news_not_current', retry=True)
                                if has_app_exception(page):
                                    raise VerificationIssue('app_exception')
                                dimensions = frame.evaluate('''() => {
                                  const main=document.querySelector('[data-testid="stMain"]');
                                  return {viewport:innerWidth,document_scroll:document.documentElement.scrollWidth,
                                    main_width:main?.clientWidth || innerWidth,
                                    main_scroll:main?.scrollWidth || document.documentElement.scrollWidth};
                                }''')
                                if not no_horizontal_overflow(dimensions):
                                    raise VerificationIssue('news_horizontal_overflow')
                                report['screens'][label] = {'count': actual_count, 'revision_matches': True,
                                                           'no_overflow': True, 'no_exception': True}
                                break
                            except VerificationIssue as error:
                                if error.blocked or not error.retry or time.monotonic() >= deadline:
                                    raise
                                page.wait_for_timeout(min(5000, max(0, deadline-time.monotonic())*1000))
                        else:
                            raise VerificationIssue('news_refresh_timeout')
                    finally:
                        context.close()
                report['status'] = 'passed'
            finally:
                browser.close()
    except VerificationIssue as error:
        report['status'] = 'blocked' if error.blocked else 'failed'
        report['errors'] = [error.code]
    except Exception:
        report['errors'] = ['news_browser_verification_unavailable']
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=APP_URL)
    parser.add_argument('--expected-version')
    parser.add_argument('--executable', default=os.environ.get('NEWS_CHROMIUM_PATH'))
    parser.add_argument('--timeout', type=float, default=240)
    args = parser.parse_args()
    args.expected_version = normalized_version(args.expected_version or (ROOT / 'VERSION').read_text().strip())
    if not args.expected_version or not 0 < args.timeout <= 300 or args.url != APP_URL:
        parser.error('Use the configured public app, exact release and bounded timeout')
    report = verify(args)
    (ROOT / 'artifacts/news_digest_live_validation.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
