"""Backfill source metadata using stored evidence only; never call a remote source."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collect_issues import annotate_existing_issues


def migrate(root=ROOT, write=False):
    path = root / 'issue_feed.csv'
    before = path.read_bytes()
    issues = pd.read_csv(path, sep='|', dtype=str).fillna('')
    watchlist = pd.read_csv(root / 'youtube_video_watchlist.csv', sep='|', dtype=str).fillna('')
    stats = pd.read_csv(root / 'youtube_video_stats.csv', sep='|', dtype=str).fillna('')
    enriched = annotate_existing_issues(issues, watchlist, stats)
    if len(enriched) != len(issues):
        raise ValueError('Metadata migration must preserve the number of stored rows')
    if enriched['date'].tolist() != issues['date'].tolist():
        raise ValueError('Metadata migration must preserve original capture dates')
    report = {
        'mode': 'write' if write else 'dry_run', 'rows': len(enriched),
        'original_sha256': hashlib.sha256(before).hexdigest(),
        'metrics_rows': int(enriched['metrics_json'].ne('').sum()),
        'reference_date_rows': int(enriched['reference_date'].ne('').sum()),
        'verification': 'stored evidence only; no live source comparison',
    }
    if write:
        temporary = path.with_suffix('.csv.tmp')
        enriched.to_csv(temporary, sep='|', index=False)
        temporary.replace(path)
        report['result_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='Replace issue_feed.csv atomically; default is dry run')
    args = parser.parse_args()
    print(json.dumps(migrate(write=args.write), ensure_ascii=False, indent=2))
