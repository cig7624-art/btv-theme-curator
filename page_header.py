"""Shared, escaped page introductions for every home workspace."""
from __future__ import annotations

import html


_LEGACY_CLASSES = {
    "story": "magazine-hero",
    "recommendation": "recommendation-heading",
    "director": "director-heading",
    "ott": "ott-page-heading",
    "media": "media-insights-heading",
}

PAGE_HEADER_CSS = """
header.page-header {
  position: relative; display: block; isolation: auto; min-width: 0;
  min-height: 94px; padding: 2px 0 14px; margin: 0;
  border: 0; overflow: visible; box-sizing: border-box;
}
.page-header .page-header-copy { min-width: 0; max-width: 100%; }
.page-header .page-header-eyebrow {
  display: block; margin: 0; color: var(--accent, #e7bd86);
  font-size: 9px; line-height: 1.5; font-weight: 650; letter-spacing: 1.8px;
}
[data-testid="stMarkdownContainer"] .page-header .page-header-title {
  color: var(--ink, #f3eee5); font-size: 28px; line-height: 1.25;
  font-weight: 600; letter-spacing: -.8px; padding: 0; margin: 5px 0;
  word-break: keep-all; overflow-wrap: anywhere;
}
[data-testid="stMarkdownContainer"] .page-header .page-header-description {
  color: var(--muted, #aaa99f); font-size: 12px; line-height: 1.65;
  margin: 0; padding: 0; word-break: keep-all; overflow-wrap: anywhere;
}
@media (max-width: 640px) {
  header.page-header { min-height: 106px; padding-top: 0; padding-bottom: 12px; }
  [data-testid="stMarkdownContainer"] .page-header .page-header-title {
    font-size: 23px; line-height: 1.3; letter-spacing: -.6px; margin: 5px 0;
  }
  [data-testid="stMarkdownContainer"] .page-header .page-header-description {
    min-height: 39.6px; font-size: 12px; line-height: 1.65;
  }
}
"""


def page_header_styles_html():
    """Inject after a page's own stylesheet, including standalone view tests."""
    return "<style>" + PAGE_HEADER_CSS + "</style>"


def page_header_html(page_id, title, description, *, eyebrow=""):
    """Use the same heading semantics and structure while retaining UI markers."""
    escape = lambda value: html.escape(str(value or ""), quote=True)
    legacy = _LEGACY_CLASSES.get(page_id, "")
    classes = "page-header" + (" " + legacy if legacy else "")
    label = ('<span class="page-header-eyebrow">' + escape(eyebrow) + '</span>') if eyebrow else ""
    return ('<header class="' + classes + '" data-page-header="' + escape(page_id) + '">'
            '<div class="page-header-copy">' + label
            + '<h1 class="page-header-title">' + escape(title) + '</h1>'
            + '<p class="page-header-description">' + escape(description) + '</p>'
            + '</div></header>')
