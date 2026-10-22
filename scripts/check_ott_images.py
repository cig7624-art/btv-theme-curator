"""Check a small sample of saved public artwork URLs without saving images."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
import requests
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ott_collection import load_snapshot, safe_http_url, utc_now_iso


def check(item):
    platform, work = item
    asset = safe_http_url(work.get('image_url'))
    result = {'platform': platform, 'title': work.get('title'), 'image_host': urlsplit(asset).hostname, 'ok': False}
    try:
        with requests.get(asset, stream=True, timeout=(5,8)) as response:
            result.update(status=response.status_code, content_type=response.headers.get('content-type',''))
            result['ok'] = response.ok and result['content_type'].split(';')[0].startswith('image/')
    except requests.RequestException as exc:
        result['error_type'] = type(exc).__name__
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', default='ott_home_snapshot.json')
    parser.add_argument('--output', default='artifacts/ott_asset_validation.json')
    args=parser.parse_args()
    jobs=[]
    for platform in load_snapshot(args.snapshot)['platforms']:
        candidates=list(platform.get('promotions',[]))
        for row in platform.get('sections',[])+platform.get('public_lists',[]):
            candidates.extend(row.get('works',[]))
        selected=[]
        for work in candidates:
            asset=safe_http_url(work.get('image_url'))
            if asset and asset not in selected:
                selected.append(asset);jobs.append((platform['id'],work))
            if len(selected)==2:break
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(check,jobs))
    output={'checked_at':utc_now_iso(),'scope':'sample_of_saved_public_image_urls','sampled':len(results),'reachable':sum(item['ok'] for item in results),'results':results}
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(output,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
