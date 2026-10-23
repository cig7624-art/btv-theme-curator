"""Small editorial guardrails for newly generated curation headings.

These checks catch recognizable collection reports and plot checklists. They do
not attempt to judge Korean prose with a word-count formula, rewrite titles, or
ban evocative headings. The model supplies the editorial judgement and shared
content rule; this module only provides a deterministic final guard.
"""
from __future__ import annotations

from collections.abc import Mapping
import re
import unicodedata


_METRIC = r"(?:조회(?:수)?|검색량|댓글(?:수)?|관객\s*수|시청률|화제성|인기\s*순위)"
_CHANGE = r"(?:늘(?:어난|어|었)|증가|급증|폭증|상승|올라|오른|급등|많(?:아|은)|높(?:아|은))"
_REPORT_PATTERNS = (
    re.compile(_METRIC + r".{0,20}" + _CHANGE),
    re.compile(_CHANGE + r".{0,12}" + _METRIC),
    re.compile(_METRIC + r".{0,8}(?:집계|추이|추적|분석|기준|비교|상위|랭킹)"),
    re.compile(_METRIC + r"\s*(?:[0-9,.]+\s*(?:만|억|천)?\s*(?:회|명|%|위)|TOP\s*\d)", re.I),
    re.compile(r"(?:클립|예고(?:편)?|선공개).{0,12}(?:반응|화제).{0,8}(?:늘|증가|급증|상승|얻|모은)"),
)
_PUBLICITY_ONLY = re.compile(
    r"^(?:(?:요즘|지금|최근|이번\s*주|다시)\s*)?"
    r"(?:화제(?:를\s*모은|가\s*된|의)?|인기(?:를\s*얻은|가\s*많은|의)?|"
    r"주목(?:을\s*받는|받는)|핫한|반응(?:이\s*좋은|을\s*얻은))\s*"
    r"(?:작품|콘텐츠|영화|드라마|영상)(?:들|모음|추천)?[.!?…]*$"
)
_GENERIC_ONLY = re.compile(
    r"^(?:(?:이번\s*주|오늘|지금|요즘)\s*)?"
    r"(?:추천|추천작|테마|콘텐츠|작품|영화|드라마|모음|컬렉션|인기작)"
    r"(?:\s*(?:추천|모음|컬렉션|테마))?[.!?…]*$"
)
_PLOT_DOMAINS = (
    re.compile(r"이혼|파경|별거"),
    re.compile(r"새\s*사랑|재혼|연애|첫사랑"),
    re.compile(r"복수|보복"),
    re.compile(r"탈옥|도주|도망"),
    re.compile(r"상속|재산\s*다툼"),
    re.compile(r"납치|유괴"),
)
_PLOT_CONNECTOR = re.compile(r"(?:\s(?:뒤|후)\s|하고\s|해서\s|으며\s|다가\s|과\s|와\s)")
_CHARACTER_END = re.compile(r"(?:한|하는|된|되는|찾은|얻은|꿈꾸는)\s*(?:주인공|사람|이들|남자|여자)(?:들)?[.!?…]*$")
_TOPIC = re.compile(
    r"가족|부부|부모|아이|형제|자매|친구|연인|사랑|로맨스|복수|추리|수사|미스터리|미스테리|"
    r"스릴러|액션|공포|코미디|판타지|모험|범죄|법정|성장|청춘|추억|고전|명작|"
    r"여행|음악|스포츠|직장|학교|전쟁|생존|우주|시간\s*여행|애니메이션|다큐|사극|SF",
    re.I,
)
_OCCASION = re.compile(r"비\s*오는|눈\s*오는|장마|가을|여름|겨울|봄날|온\s*가족|퇴근|휴일|주말|연휴|잠들기|혼자\s*보")
_COMPARISON = re.compile(r"(?:처럼|같은|비슷한|닮은).{0,18}(?:영화|드라마|액션|스릴러|로맨스|공포|코미디|작품)")
_DISTRIBUTION_REPORT = re.compile(
    r"(?:OTT|넷플릭스|티빙|웨이브|디즈니|쿠팡플레이|온라인|스트리밍|재개봉|재공개)"
    r".{0,16}(?:공개|개봉|서비스)?\s*(?:후|뒤|이후).{0,30}"
    r"(?:역주행|재조명|입소문|다시\s*(?:뜬|주목)|반응을?\s*(?:얻|모)|인기를?\s*얻)"
    r".{0,12}(?:영화|드라마|콘텐츠|작품|영상)(?:들|\s*모음)?[.!?…]*$",
    re.I,
)
_NAMED_PERIOD = re.compile(r"(?:19|20)\d{2}\s*년|\d{2}\s*년대|세기말|밀레니엄")


def _text(value):
    if value is None or not isinstance(value, (str, int, float)):
        return ""
    result = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip()
    return "" if result.lower() in {"nan", "none"} else result


def title_review(candidate: Mapping) -> dict:
    """Return ``eligible``, a stable reason code, and an internal clarity value.

    ``theme_name`` is assessed as the reader sees it. Optional ``copy``, ``genre``,
    ``selection_rule`` and ``reference_titles`` support concise editorial titles,
    but cannot make a metric report into a content theme. Scores are rough local
    ordering hints, never a measured user-facing quality or popularity score.
    """
    if not isinstance(candidate, Mapping):
        return {"eligible": False, "reason": "missing_name", "clarity": 0}
    name = _text(candidate.get("theme_name"))
    if len(re.sub(r"[^0-9a-zA-Z가-힣]", "", name)) < 3:
        return {"eligible": False, "reason": "missing_name", "clarity": 0}
    if any(pattern.search(name) for pattern in _REPORT_PATTERNS):
        return {"eligible": False, "reason": "observation_report", "clarity": 0}
    if (_DISTRIBUTION_REPORT.search(name)
            and not (_TOPIC.search(name) or _OCCASION.search(name) or _NAMED_PERIOD.search(name))):
        return {"eligible": False, "reason": "observation_report", "clarity": 0}
    if _PUBLICITY_ONLY.fullmatch(name):
        return {"eligible": False, "reason": "publicity_only", "clarity": 10}
    if _GENERIC_ONLY.fullmatch(name):
        return {"eligible": False, "reason": "unspecified_topic", "clarity": 10}
    plot_axes = sum(bool(pattern.search(name)) for pattern in _PLOT_DOMAINS)
    # One relationship or two contrasting feelings can be a useful theme. An
    # ordered chain of three separate plots ending in "a protagonist who..."
    # reads as a synopsis checklist instead of a reusable viewing proposition.
    if plot_axes >= 3 and len(_PLOT_CONNECTOR.findall(name)) >= 2 and _CHARACTER_END.search(name):
        return {"eligible": False, "reason": "plot_checklist", "clarity": 25}

    clear_topic = bool(_TOPIC.search(name) or _OCCASION.search(name) or _COMPARISON.search(name))
    clarity = 86 if clear_topic else 72
    # A short, evocative heading may rely on its copy and actual content rule.
    # A metadata list never earns more points merely by getting longer.
    rule = _text(candidate.get("selection_rule") or candidate.get("content_selection_rule"))
    context = bool(_text(candidate.get("genre")) and (_text(candidate.get("copy")) or rule))
    if context and not clear_topic:
        clarity += 6
    if 4 <= len(name) <= 32:
        clarity += 4
    elif len(name) > 42:
        clarity -= 12
    return {"eligible": True, "reason": "", "clarity": min(94, max(58, clarity))}
