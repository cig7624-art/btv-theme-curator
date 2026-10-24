"""Official domestic media sources with separate media and product selection.

Registration is not proof that an endpoint is available. The collector records
actual retrieval results, publication dates and source paragraphs independently.
This module never supplies prices, effective dates, or service availability.
"""
from __future__ import annotations

from collections.abc import Mapping
import re


# Provider-owned public newsrooms/blogs, not third-party product comparison sites.
# The source collector must still verify the returned document before publishing.
DOMESTIC_MEDIA_SOURCES = (
    {"id": "skb_products", "name": "SK브로드밴드 공식 블로그", "company": "SK브로드밴드",
     "url": "https://blog.bworld.co.kr/category/%EB%B3%B4%EB%8F%84%EC%9E%90%EB%A3%8C/feed/", "source_type": "official_media_announcement",
     "country": "KR", "market_scope": "domestic_media", "require_domestic_media": True,
     "allowed_hosts": ["blog.bworld.co.kr"],
     "image_hosts": ["blog.bworld.co.kr", "i0.wp.com", "i1.wp.com", "i2.wp.com"],
     "article_selector": ".elementor-widget-theme-post-content",
     "document_urls": [{"url": "https://blog.bworld.co.kr/2026/07/01/btv-plus-max-all-in-one-ott-subscription/",
                        "title": "SKB, 지상파·실시간 채널 모은 올인원 구독상품 ‘B tv+ max’ 출시"}]},
    {"id": "kt_products", "name": "KT 공식 보도자료", "company": "KT",
     "url": "https://corp.kt.com/html/promote/news/report_list.html", "source_type": "official_media_announcement",
     "country": "KR", "market_scope": "domestic_media", "require_domestic_media": True,
     "allowed_hosts": ["corp.kt.com"], "image_hosts": ["corp.kt.com"]},
    {"id": "lgu_products", "name": "LG유플러스 뉴스룸", "company": "LG유플러스",
     "url": "https://news.lguplus.com/category/%ED%94%84%EB%A0%88%EC%8A%A4%EC%84%BC%ED%84%B0/%EB%B3%B4%EB%8F%84%EC%9E%90%EB%A3%8C", "source_type": "official_media_announcement",
     "country": "KR", "market_scope": "domestic_media", "require_domestic_media": True,
     "allowed_hosts": ["news.lguplus.com"], "image_hosts": ["news.lguplus.com"],
     "article_selector": ".entry-body-wrap", "title_selector": ".entry-header-title",
     "discovery_urls": ["https://news.lguplus.com/category/uplus-%EC%9D%B8%EC%82%AC%EC%9D%B4%EB%93%9C/%EC%83%81%ED%92%88-%EC%84%9C%EB%B9%84%EC%8A%A4"],
     "document_urls": [{"url": "https://news.lguplus.com/22099",
                        "title": "유독에서 구글 AI Pro와 YouTube Premium 더욱 합리적으로 즐기세요"}]},
    {"id": "coupang_products", "name": "쿠팡 뉴스룸", "company": "쿠팡플레이",
     "url": "https://news.coupang.com/feed/", "source_type": "official_media_announcement",
     "country": "KR", "market_scope": "domestic_media", "require_domestic_media": True,
     "allowed_hosts": ["news.coupang.com"], "image_hosts": ["news.coupang.com"]},
    {"id": "cjenm_products", "name": "CJ ENM 공식 뉴스", "company": "CJ ENM",
     "url": "https://www.cjenm.com/ko/news/", "source_type": "official_media_announcement",
     "country": "KR", "market_scope": "domestic_media", "require_domestic_media": True,
     "allowed_hosts": ["www.cjenm.com", "cjenm.com"], "image_hosts": ["www.cjenm.com", "cjenm.com", "web2-cf-image.cjenm.com"],
     "article_selector": ".detail-panel .cont-area", "title_selector": ".con-header h1",
     "date_selector": ".con-header .date .tb1"},
)


# Keep stored source IDs and the original registry import compatible.
DOMESTIC_PRODUCT_SOURCES = DOMESTIC_MEDIA_SOURCES


_MEDIA = re.compile(
    r"(?<![a-z])B\s*tv(?:\s*\+)?(?![a-z])|지니\s*TV|Genie\s*TV|"
    r"U\s*(?:\+|플러스)\s*tv|유플러스\s*tv|IPTV|OTT|"
    r"티빙|TVING|웨이브|wavve|쿠팡\s*플레이|Coupang\s*Play|"
    r"디즈니\s*(?:\+|플러스)|Disney\s*(?:\+|Plus)|넷플릭스|Netflix|"
    r"유튜브\s*프리미엄|YouTube\s*Premium|유독(?=\s*(?:[,.·:]|$|요금|구독|상품|Pick|픽)|에서|의|과|은|을|이)", re.I)
_PROVIDER = re.compile(r"SK\s*브로드밴드|SKB|케이티|(?<![a-z0-9])KT(?![a-z0-9])|LG\s*유플러스|LG\s*U\s*\+", re.I)
_PRICING = re.compile(r"요금(?:제)?|구독료|구독\s*(?:상품|패키지)|이용료|월정액|정액제|가격\s*(?:인상|인하|변경)|광고형|광고\s*요금|이용권", re.I)
_BUNDLE = re.compile(r"결합(?:\s*상품)?|번들|구독\s*패키지|통합\s*(?:요금|이용권|상품)|동시\s*가입", re.I)
_PROMOTION = re.compile(r"할인|혜택|프로모션|쿠폰|캐시백|무료\s*(?:체험|이용)|무료화|반값|가입\s*선물", re.I)
_ACCESS = re.compile(r"계정\s*공유|동시\s*(?:접속|시청)|가구\s*(?:인증|공유)|이용\s*정책|시청\s*제한|로그인\s*정책|광고\s*정책|해지\s*(?:정책|수수료)", re.I)
_FEATURE = re.compile(r"셋톱(?:박스)?|리모컨|음성\s*검색|AI\s*(?:검색|추천|에이전트)|자연어\s*검색|앱\s*(?:개편|통합)|화질|멀티뷰|시청\s*(?:기능|화면)|접근성\s*기능", re.I)
_PASS = re.compile(r"스포츠\s*(?:패스|이용권|요금제|상품|패키지)|sports\s*pass", re.I)
_RETAIL = re.compile(r"요금|가격|구독|멤버십|이용권|가입|결합|번들|패키지|패스|광고형|월정액|무료\s*체험", re.I)
_CHANGE = re.compile(r"출시|선보|신설|개편|도입|시작|시행|확대|축소|추가|종료|중단|변경|인상|인하|통합|연동|적용|무료화|제공|새로운|새롭게|신규", re.I)
_GUIDE = re.compile(r"(?:알아보[기자]|총정리|(?:가입|이용|설정|활용)(?:하는)?\s*방법|추천\s*(?:요금제|영화|드라마)|정주행|시청\s*가이드|사용법|리뷰)", re.I)
_CONTENT = re.compile(r"예고편|선공개|캐스팅|제작\s*(?:발표|확정)|드라마|영화|오리지널|시리즈|방송\s*프로그램|콘서트", re.I)
_CORPORATE = re.compile(r"실적|매출|영업\s*이익|주주|주가|배당|기업\s*인수|합병|M\s*&\s*A|채용|수상|사회\s*공헌|봉사|보이스피싱", re.I)
_FOREIGN_MARKET = re.compile(r"(?:미국|일본|영국|독일|프랑스|호주|인도|동남아|해외)\s*(?:시장|현지|전용|에서|요금제|구독료|가격|출시|진출)", re.I)
_DOMESTIC = re.compile(r"한국|국내|대한민국|한국\s*가입자", re.I)
_SPECULATIVE = re.compile(r"될까|오를까|할까|가능성|전망|예상|루머|미정|추측", re.I)
_NON_MEDIA_PRODUCT = re.compile(r"휴대폰|스마트폰|아이폰|갤럭시|(?<![a-z])(?:5G|LTE)(?![a-z])|(?:기가\s*)?인터넷|와이파이|Wi-?Fi", re.I)
_LAUNCH_BODY = re.compile(r"국내\s*최초|새롭게\s*(?:선보|출시)|신규\s*(?:상품|구독|이용권|패키지)|(?:상품|패키지|이용권|구독)\S*\s*(?:출시|선보)", re.I)
_MEDIA_COMPANY = re.compile(r"CJ\s*ENM|스튜디오\s*드래곤|SLL|tvN|JTBC|MBC|SBS|KBS|ENA|EBS", re.I)
_MEDIA_CONTENT = re.compile(r"미디어|콘텐츠|방송|영화|드라마|시리즈|예능|다큐|프로그램|중계|채널|VOD|\bIP\b", re.I)
_CONTENT_EVENT = re.compile(r"신작|라인업|편성|공개|첫\s*방송|첫\s*공개|개봉|제작|론칭|런칭|선보|시즌\s*\d|성과|흥행|수상|후보|노미네이트|노미니|차트|제공|신설", re.I)
_MEDIA_PARTNERSHIP = re.compile(r"제휴|파트너십|파트너쉽|협력|공급|수출|유통|합병|인수|계약|브랜드관", re.I)
_MEDIA_EARNINGS = re.compile(r"실적|매출|영업\s*이익|수익|판매액|거래액|가입자|시청\s*시간|시청률|관객|박스\s*오피스", re.I)
_MEDIA_TECH = re.compile(r"\bAI\b|인공지능|검색|추천\s*기술|화질|멀티뷰|셋톱|접근성|스트리밍\s*기술", re.I)
_NON_MEDIA_CORPORATE = re.compile(r"채용|인적\s*자원|우수\s*기관|사회\s*공헌|봉사|어린이\s*(?:대상|체험)|제작\s*체험|보이스피싱|보안\s*인증|기부|임직원|조직\s*개편|CEO\s*선임|경영진\s*선임", re.I)


def _text(value, limit=18000):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def domestic_product_topic(title, body="", source=None):
    """Select an actual domestic retail-product change, never a routine title.

    Titles are also used for link discovery, so an explicit product subject is
    required in the headline. Body/footer words alone cannot promote a trailer
    or a general corporate story into a product announcement.
    """
    title, body = _text(title, 600), _text(body)
    if not title:
        return ""
    if _SPECULATIVE.search(title):
        return ""
    if _FOREIGN_MARKET.search(title) and not _DOMESTIC.search(title):
        return ""
    explicit_product = bool(_PRICING.search(title) or _BUNDLE.search(title) or _ACCESS.search(title)
                            or _FEATURE.search(title) or _PASS.search(title))
    media_subject = bool(_MEDIA.search(title))
    if not media_subject and _NON_MEDIA_PRODUCT.search(title):
        # A mobile/broadband announcement may mention IPTV in a boilerplate
        # paragraph. Only an explicit media subject in its headline qualifies.
        return ""
    if not media_subject and _PROVIDER.search(title) and explicit_product:
        # A generic KT/LGU+/SKB mobile or broadband tariff is outside this tab.
        media_subject = bool(((_PRICING.search(title) or _BUNDLE.search(title)) and _MEDIA.search(body[:800]))
                             or re.search(r"광고형|스포츠\s*패스|셋톱|TV\s*결합", title, re.I))
    if not media_subject:
        return ""
    if _CORPORATE.search(title) and not explicit_product:
        return ""
    if _CONTENT.search(title) and not (_PRICING.search(title) or _BUNDLE.search(title) or _PASS.search(title)
                                      or _ACCESS.search(title) or _FEATURE.search(title)):
        return ""
    if _GUIDE.search(title) and not re.search(r"인상|인하|변경|개편|신설|출시|종료|시행", title):
        return ""
    if not _CHANGE.search(title):
        # Some official product introductions use a service-led headline
        # ('유독에서 ... 즐기세요'). Only an explicit, newly announced bundle
        # in the opening source paragraphs can qualify that headline. A list
        # of existing subscriber benefits or a how-to guide still cannot.
        opening = body[:1600]
        if (media_subject and not _GUIDE.search(title) and not _CONTENT.search(title)
                and _LAUNCH_BODY.search(opening) and _BUNDLE.search(opening)
                and _RETAIL.search(opening)):
            return "partnership_distribution"
        return ""
    if _PRICING.search(title):
        return "advertising_pricing"
    if _BUNDLE.search(title):
        return "partnership_distribution"
    if _PASS.search(title):
        return "sports_content"
    if _ACCESS.search(title):
        return "advertising_pricing"
    if _FEATURE.search(title):
        return "ai_technology"
    if _PROMOTION.search(title) and _RETAIL.search(title):
        return "advertising_pricing"
    return ""


classify_domestic_product = domestic_product_topic


def domestic_product_candidate(title):
    """Discovery hint only; actual source body must still pass selection.

    A newsroom listing may omit launch wording. This permits fetching that
    exact article without promoting the listing/excerpt into a selected item.
    """
    title = _text(title, 600)
    if not title or _SPECULATIVE.search(title) or _GUIDE.search(title):
        return False
    if _FOREIGN_MARKET.search(title) and not _DOMESTIC.search(title):
        return False
    if _NON_MEDIA_PRODUCT.search(title) and not _MEDIA.search(title):
        return False
    product = bool(_PRICING.search(title) or _BUNDLE.search(title) or _ACCESS.search(title)
                   or _FEATURE.search(title) or _PASS.search(title) or _RETAIL.search(title)
                   or re.search(r"YouTube\s*Premium|유튜브\s*프리미엄|유독(?:에서|\s|,)", title, re.I))
    return bool(product and (_MEDIA.search(title) or _PROVIDER.search(title))
                and (not _CONTENT.search(title) or _PRICING.search(title) or _BUNDLE.search(title)))


def domestic_media_topic(title, body="", source=None):
    """Classify actual domestic media news, including content and industry.

    A domestic publisher may announce overseas distribution of Korean content;
    that is still a domestic media-company issue. This flag describes the
    source/story, never the country in which a subscription can be purchased.
    Product eligibility remains a separate, stricter decision above.
    """
    title, body = _text(title, 600), _text(body)
    if not title or _SPECULATIVE.search(title) or _NON_MEDIA_CORPORATE.search(title):
        return ""
    product = domestic_product_topic(title, body, source)
    if product:
        return product
    if _GUIDE.search(title):
        return ""
    headline_service = bool(_MEDIA.search(title) or _MEDIA_COMPANY.search(title))
    headline_content = bool(_MEDIA_CONTENT.search(title))
    if _NON_MEDIA_PRODUCT.search(title) and not headline_service:
        return ""
    if not (headline_service or headline_content):
        return ""
    # A provider-led 'new programs' headline needs the fetched opening article
    # paragraphs to confirm actual media distribution, not a welfare program.
    confirmed_context = headline_service or bool((_MEDIA.search(body[:2400]) or _MEDIA_COMPANY.search(body[:2400]))
                                                  and _MEDIA_CONTENT.search(body[:2400]))
    if not confirmed_context:
        return ""
    if _MEDIA_PARTNERSHIP.search(title):
        return "partnership_distribution"
    if _MEDIA_EARNINGS.search(title):
        return "earnings_strategy"
    if _MEDIA_TECH.search(title):
        return "ai_technology"
    if ((headline_content or _MEDIA_CONTENT.search(body[:1200]))
            and _CONTENT_EVENT.search(title)):
        return "sports_content"
    if headline_service and re.search(r"중계권|스포츠|KBO|NBA|NFL|프리미어\s*리그|월드컵|올림픽", title, re.I):
        return "sports_content"
    return ""


def domestic_media_candidate(title):
    """Permit fetching a media headline; never use a listing as final proof."""
    title = _text(title, 600)
    if not title or _GUIDE.search(title) or _SPECULATIVE.search(title) or _NON_MEDIA_CORPORATE.search(title):
        return False
    if domestic_product_candidate(title):
        return True
    headline_service = bool(_MEDIA.search(title) or _MEDIA_COMPANY.search(title))
    if _NON_MEDIA_PRODUCT.search(title) and not headline_service:
        return False
    subject = headline_service or bool(_MEDIA_CONTENT.search(title))
    event = bool(_CONTENT_EVENT.search(title) or _MEDIA_PARTNERSHIP.search(title)
                 or _MEDIA_EARNINGS.search(title) or _MEDIA_TECH.search(title))
    return bool(subject and event)


def is_domestic_media_item(item):
    """Source-grounded domestic media and genuine product announcements."""
    if not isinstance(item, Mapping):
        return False
    if item.get("market_scope") in {"domestic_media", "domestic_product"} or item.get("domestic_product") is True:
        return True
    source_ids = {source["id"] for source in DOMESTIC_MEDIA_SOURCES}
    return bool(item.get("source_id") in source_ids and item.get("country") == "KR"
                and domestic_media_topic(item.get("title"), item.get("body_text", "")))


def is_domestic_product_item(item):
    """Only positively marked/verified source stories belong in the product tab."""
    if not isinstance(item, Mapping):
        return False
    if item.get("market_scope") == "domestic_product" or item.get("domestic_product") is True:
        return True
    source_ids = {source["id"] for source in DOMESTIC_PRODUCT_SOURCES}
    return bool(item.get("source_id") in source_ids and item.get("country") == "KR"
                and domestic_product_topic(item.get("title"), item.get("body_text", "")))
