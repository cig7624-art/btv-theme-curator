"""Separate newspaper corner labels from named film/TV subjects."""
import re


_KNOWN_LABEL = re.compile(r"^(?:TEN스타필드|TEN초점|기자수첩|취재수첩|데스크칼럼|연예프리즘|인터뷰[①-⑳\d]*)$", re.I)
_BRACKET = re.compile(r"\[([^\]]+)\]")


def strip_news_editorial_labels(text):
    """Return cleaned text and removed labels, preserving explicit work brackets.

    Trailing square brackets are a common editorial suffix, including previously
    unseen corner names. A leading title, or a title immediately introduced by a
    media noun, remains usable. An optional RSS publisher suffix is ignored.
    """
    text = str(text or "")
    removed = []

    def replace(match):
        label = match.group(1).strip()
        before, after = text[:match.start()].strip(), text[match.end():].strip()
        introduced_work = bool(re.search(r"(?:영화|드라마|예능|시리즈|작품)\s*$", before))
        trailing_label = bool(before) and (not after or after.startswith("- ")) and not introduced_work
        # Regional/desk prefixes can precede a separately, explicitly named
        # film: [임실군] ‘우리마을담다’ 동화영화. Prefer the actual media title.
        leading_desk = not before and bool(re.match(
            r"[‘'\"“`]([^’'\"”`]+)[’'\"”`]\s*(?:동화영화|영화|드라마|예능|시리즈|작품)", after))
        if _KNOWN_LABEL.fullmatch(label) or trailing_label or leading_desk:
            removed.append(label)
            # The label itself is not a work, but this context is needed to
            # recognize a leading quoted work followed by an actor interview.
            return " 인터뷰 " if label.startswith("인터뷰") else " "
        return match.group()

    return _BRACKET.sub(replace, text), removed
