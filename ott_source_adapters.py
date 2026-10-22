"""Narrow adapters verified against official public home documents on 2026-10-22.

These read explicit homepage collections, never arbitrary JSON title keys. The
source remains the public homepage; no account/session endpoint is requested.
"""
from __future__ import annotations
import hashlib
import json
import re
import unicodedata
from urllib.parse import urljoin, urlsplit


def text(value):
    return unicodedata.normalize('NFC', re.sub(r'\s+', ' ', str(value or '')).strip())[:500]


def url(value, base):
    value = str(value or '').strip()
    if not value or any(ord(c) < 32 for c in value):
        return ''
    try:
        result = urljoin(base, value)
        parts = urlsplit(result)
        if parts.scheme not in {'https', 'http'} or not parts.hostname or parts.username or parts.password:
            return ''
        parts.port
        return result
    except ValueError:
        return ''


def image(node, base):
    if node is None:
        return ''
    for key in ['src', 'data-src', 'data-original']:
        value = url(node.get(key), base)
        if value:
            return value
    return ''


def row(service, title, works, position, **extra):
    return {'id': hashlib.sha256((service+'|'+title).encode()).hexdigest()[:16],
            'title': text(title), 'description': '', 'kind': 'other',
            'position': position, 'url': '', 'works': works[:8],
            'works_complete': False, 'source_position_verified': True, **extra}


def extract_tving(soup, base):
    """TVING publishes the same ordered home groups as CollectionPage JSON-LD."""
    groups = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get('@type') != 'CollectionPage':
            continue
        canonical = url(data.get('url'), base)
        if urlsplit(canonical).hostname not in {'www.tving.com', 'tving.com'} or urlsplit(canonical).path not in {'', '/'}:
            continue
        entity = data.get('mainEntity', {})
        if isinstance(entity, dict) and entity.get('@type') == 'ItemList' and isinstance(entity.get('itemListElement'), list):
            groups = entity['itemListElement']
            break
    if groups is None:
        return None
    sections, promotions, seen = [], [], set()
    for group_index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        listing = group.get('item', {})
        if not isinstance(listing, dict) or listing.get('@type') != 'ItemList':
            continue
        title = text(listing.get('name') or group.get('name'))
        entries = listing.get('itemListElement', [])
        if not title or not isinstance(entries, list):
            continue
        works, known = [], set()
        for entry in entries:
            item = entry.get('item', {}) if isinstance(entry, dict) else {}
            if not isinstance(item, dict) or not text(item.get('name')):
                continue
            target = url(item.get('url'), base)
            if urlsplit(target).hostname not in {'www.tving.com', 'tving.com'} or target in known:
                continue
            known.add(target)
            art = item.get('image', '')
            if isinstance(art, dict):art = art.get('url', '')
            if isinstance(art, list):art = art[0] if art else ''
            works.append({'title': text(item['name']), 'url': target,
                          'image_url': url(art, base), 'description': text(item.get('description'))})
        if not works or title in seen:
            continue
        seen.add(title)
        is_main = group_index == 0 and title == 'TVING 추천 콘텐츠' and any('/operation/banner/' in w['image_url'] for w in works)
        if is_main:
            promotions = [dict(w, position=i+1, placement='메인 배너') for i, w in enumerate(works[:5])]
            continue
        position = group_index if promotions else group_index+1
        if position > 10:
            break
        kind = 'live' if all('/live/' in w['url'] for w in works) else 'shorts' if '쇼츠' in title else 'clips' if '미방분' in title else 'promotion' if len(works)==1 else ''
        sections.append(row('tving', title, works, position, kind=kind or 'theme', source_format='official_home_jsonld'))
    return {'sections': sections, 'promotions': promotions, 'public_lists': [], 'marketing': False}


def extract_netflix(soup, base):
    region = soup.select_one('[data-uia="nmhp-top-10"]')
    if region is None:
        return None
    heading = region.find(['h1', 'h2', 'h3'])
    works = []
    for button in region.select('button[data-uia^="top-10-element-"]'):
        content = button.find('div', recursive=False)
        name = text(content.get_text(' ', strip=True)) if content else ''
        art = button.select_one('[style*="background-image"]')
        match = re.search(r'url\([\"\']?(.*?)(?:[\"\']?\)|$)', art.get('style', '')) if art else None
        artwork = match.group(1).rstrip('"\'') if match else ''
        # Old diagnostic captures escaped the closing CSS delimiter; live DOM
        # uses a normal ). Do not attach it to the actual public image URL.
        if artwork.endswith('%29'):artwork = artwork[:-3]
        if name:
            works.append({'title': name, 'url': '', 'image_url': url(artwork, base)})
    if not heading or not works:
        return None
    return {'sections': [], 'promotions': [], 'public_lists': [row('netflix', heading.get_text(' ', strip=True), works, 1, kind='ranking', placement='공개 인기 목록')], 'marketing': True}


def extract_disney(soup, base):
    gallery = soup.select_one('[data-testid="hero-gallery"]')
    sections, promotions = [], []
    if gallery:
        for index, tile in enumerate(gallery.select('[data-testid^="gallery-entry-"]')[:5]):
            description = gallery.select_one(f'[data-testid="description-{index}"]')
            if not description:
                continue
            name = text(description.get_text(' ', strip=True))
            name = re.sub(r'\s*(?:지금 스트리밍 중|지금 스트리밍중|지금 공개 중)\s*$', '', name)
            if name:
                promotions.append({'title': name, 'url': '', 'image_url': image(tile.find('img'), base), 'position': index+1, 'placement': '공개 메인 배너'})
    for heading in soup.select('h2'):
        name = text(heading.get_text(' ', strip=True))
        if not re.search(r'TOP\s*10|컬렉션', name, re.I):
            continue
        region = heading.find_parent('section')
        if region is None:
            continue
        is_collection = name == '컬렉션'
        works, seen = [], set()
        for anchor in region.select('a[href]'):
            target = url(anchor.get('href'), base)
            path = urlsplit(target).path
            if urlsplit(target).hostname not in {'www.disneyplus.com', 'disneyplus.com'}:
                continue
            if ('/browse/page-' if is_collection else '/browse/entity-') not in path:
                continue
            img = anchor.find('img')
            title = text(anchor.get('aria-label') or (img.get('alt') if img else ''))
            if not title or target in seen:
                continue
            seen.add(target)
            works.append({'title': title, 'url': target, 'image_url': image(img, base)})
        if works:
            sections.append(row('disney', name, works, len(sections)+1,
                                kind='collection' if is_collection else 'ranking',
                                items_kind='collections' if is_collection else 'works',
                                placement='공개 홍보 화면의 목록'))
    if not gallery and not sections:
        return None
    return {'sections': [], 'public_lists': sections, 'promotions': promotions, 'marketing': True}


def extract_wavve_dom(soup, base):
    """Wavve title-area is a real shelf even while lazy title cards are pending."""
    headings = soup.select('h1.title-area, h1.highlight-band-txt')
    if not headings:
        return None
    rows = []
    for index, heading in enumerate(headings[:10]):
        region = heading.find_parent('div', class_='multi')
        if region is None:
            continue
        subtitle = region.select_one('.highlight-band-subtitle')
        name = text((subtitle or heading).get_text(' ', strip=True))
        if not name:
            img = heading.find('img')
            name = text(img.get('alt') if img else '')
        if not name:
            continue
        works, seen = [], set()
        for anchor in region.select('.thumb a[aria-label]'):
            name_work = text(anchor.get('aria-label'))
            if not name_work or name_work in seen:
                continue
            seen.add(name_work)
            works.append({'title': name_work, 'url': url(anchor.get('href'), base),
                          'image_url': image(anchor.find('img'), base)})
        rows.append(row('wavve', name, works, index+1, source_format='public_home_dom', works_status='observed' if works else 'not_loaded'))
    return {'sections': rows, 'promotions': [], 'public_lists': [], 'marketing': False}


def extract_provider(soup, platform, base):
    function = {'tving': extract_tving, 'netflix': extract_netflix,
                'disney': extract_disney, 'wavve': extract_wavve_dom}.get(platform['id'])
    return function(soup, base) if function else None


def enrich_wavve_public_responses(adapted, responses, base):
    """Join observed v1/home layout and its v1/catalog responses by catalog code.

    The browser itself requests these anonymous public homepage resources. We
    neither discover private endpoints nor turn unrelated JSON into collections.
    """
    if not adapted or not isinstance(responses, list):
        return adapted
    layouts, catalogs = [], {}
    for response in responses[:40]:
        if not isinstance(response, dict):
            continue
        envelope = response.get('data', {})
        data = envelope.get('data', {}) if isinstance(envelope, dict) else {}
        if not isinstance(data, dict):
            continue
        if response.get('url_path') == '/v1/home' and data.get('type') == 'layout_home':
            layouts = data.get('layout', {}).get('catalog', {}).get('catalog_list', [])
        if response.get('url_path') == '/v1/catalog' and data.get('type') == 'catalog' and response.get('catalog_code'):
            catalogs[response['catalog_code']] = data
    by_title, banners = {}, []
    for group in layouts if isinstance(layouts, list) else []:
        if not isinstance(group, dict):continue
        title = next((text(e.get('property', {}).get('text')) for e in group.get('element_list', []) if e.get('type') == 'title'), '')
        code = group.get('id')
        if title:by_title[title] = code
        if group.get('description') == '메인 홈 빅 배너':
            banners = catalogs.get(code, {}).get('context_list', [])
    for shelf in adapted['sections']:
        data = catalogs.get(by_title.get(shelf['title']), {})
        entries = data.get('context_list', [])
        if not isinstance(entries, list):continue
        works, seen = [], set()
        for entry in entries:
            if not isinstance(entry, dict) or entry.get('context_type') == 'banner':continue
            def obj(key):
                value = entry.get(key, {})
                return value if isinstance(value, dict) else {}
            manual, program, series, movie, content = (obj(k) for k in ['manualband', 'program', 'series', 'movie', 'content'])
            title = text(manual.get('title1') or program.get('title') or series.get('title') or movie.get('title') or content.get('title'))
            if not title or title in seen:continue
            seen.add(title)
            art = manual.get('image') or program.get('vertical_logo_y_image') or series.get('vertical_logo_y_image') or movie.get('vertical_logo_y_image') or content.get('content_image')
            # JavaScript click handlers do not expose a stable title URL here.
            # Preserve the title/artwork and link its evidence to the real home.
            works.append({'title': title, 'url': '', 'image_url': url(art, base)})
        if works:
            shelf['works'] = works[:8]
            shelf['works_status'] = 'observed'
            subtitle = text(data.get('top_context', {}).get('subtitle'))
            if subtitle:shelf['description'] = subtitle
            if len(works) == 1:shelf['kind'] = 'promotion'
    promotions, seen = [], set()
    for entry in banners if isinstance(banners, list) else []:
        banner = entry.get('banner', {}) if isinstance(entry, dict) else {}
        if not isinstance(banner, dict):continue
        name = text(banner.get('name'))
        if not name or name in seen:continue
        seen.add(name)
        promotions.append({'title': name, 'url': url(banner.get('url'), base),
                           'image_url': url(banner.get('landscape_background') or banner.get('landscape_layer'), base),
                           'position': len(promotions)+1, 'placement': '공개 메인 배너'})
    if promotions:adapted['promotions'] = promotions[:5]
    return adapted
