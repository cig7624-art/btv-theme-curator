"""Exercise actual app helpers without starting Streamlit or loading local CSVs."""
import ast
from functools import partial
import html
from pathlib import Path
import re
import unittest
from urllib.parse import parse_qs, quote, urlparse

import pandas as pd

from issue_presentation import present_issue
from issue_quality import BOXOFFICE, NEWS, YOUTUBE, assess_issues, build_ranked_issues
from issue_related_news import attach_related_news


NOW = "2026-10-21T12:00:00+09:00"
APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def app_helpers():
    names = {"safe_url", "build_core_issues", "related_news_html", "issue_evidence_html", "news_digest_html"}
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"), filename=str(APP_PATH))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in selected} != names:
        raise AssertionError("Expected app helpers are missing")
    namespace = dict(pd=pd, html=html, re=re, urlparse=urlparse, parse_qs=parse_qs, quote=quote,
                     build_ranked_issues=build_ranked_issues, present_issue=present_issue,
                     attach_related_news=partial(attach_related_news, now=NOW))
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(APP_PATH), "exec"), namespace)
    return namespace


def news(**changes):
    row = dict(date="2026-10-21", source=NEWS, source_type="news", related_content="실낙원",
               issue_title="'실낙원' 21일 개봉", source_url="https://ent.example.com/article/1",
               published_at="2026-10-20", publisher="연예일보", description="실낙원 개봉 소식",
               event_type="release", keywords="영화,개봉")
    row.update(changes)
    return row


def youtube(**changes):
    row = dict(date="2026-10-21", source=YOUTUBE, source_type="youtube_reaction", related_content="실낙원",
               issue_title="[실낙원] 공식 예고편", source_url="https://www.youtube.com/watch?v=abcdefghijk",
               published_at="2026-10-20", publisher="영화 채널", keywords="영화,예고편",
               metrics_json=dict(views_total=300000, views_change=100000,
                                 observation_start="2026-10-20", observation_end="2026-10-21"))
    row.update(changes)
    return row


class IssueAppIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = app_helpers()

    def assessed(self, rows):
        return assess_issues(pd.DataFrame(rows), now=NOW)

    def test_source_filter_preserves_same_work_article_below_original_link(self):
        assessed = self.assessed([youtube(), news(), news(related_content="오디세이", issue_title="'오디세이' 개봉", source_url="https://ent.example.com/article/2")])
        filtered = assessed[assessed.source_group == YOUTUBE]
        ranked = self.helpers["build_core_issues"](filtered, news_pool=assessed)
        self.assertEqual(len(ranked), 1)
        selected = ranked.iloc[0]
        self.assertEqual(selected.source_group, YOUTUBE)
        self.assertEqual(len(selected.related_news), 1)
        rendered = self.helpers["issue_evidence_html"](selected)
        self.assertLess(rendered.index("원문에서 확인"), rendered.index("함께 읽을 관련 기사"))
        self.assertIn("https://ent.example.com/article/1", rendered)
        self.assertIn("연예일보 · 2026-10-20", rendered)
        self.assertNotIn("https://ent.example.com/article/2", rendered)

    def test_boxoffice_card_gets_related_reporting_without_changing_audience_reason(self):
        row = youtube(source=BOXOFFICE, source_type="boxoffice", source_url="https://www.kobis.or.kr/chart",
                      issue_title="실낙원 박스오피스 1위", reference_date="2026-10-20", published_at="",
                      metrics_json=dict(rank=1, daily_audience=22474))
        assessed = self.assessed([row, news()])
        ranked = self.helpers["build_core_issues"](assessed[assessed.source_group == BOXOFFICE], news_pool=assessed)
        selected = ranked.iloc[0]
        rendered = self.helpers["issue_evidence_html"](selected)
        self.assertIn("22,474", rendered)
        self.assertIn("영화진흥위원회", rendered)
        self.assertIn("함께 읽을 관련 기사", rendered)
        self.assertEqual(selected.confirmed_route_count, 1)

    def test_news_digest_has_its_own_reason_and_does_not_link_to_itself_twice(self):
        assessed = self.assessed([youtube(), news(), news(issue_title="'실낙원' 배우 인터뷰", source_url="https://ent.example.com/article/2", description="실낙원 배우 인터뷰", event_type="interview")])
        ranked = self.helpers["build_core_issues"](assessed[assessed.source_group == NEWS], news_pool=assessed)
        self.assertEqual(len(ranked), 1)
        selected = ranked.iloc[0]
        rendered = self.helpers["news_digest_html"](selected)
        self.assertIn("news-digest-card", rendered)
        self.assertIn("선정 근거", rendered)
        self.assertIn("기사 발행일", rendered)
        self.assertEqual(rendered.count('href="' + selected.source_url + '"'), 1)
        self.assertIn("함께 읽을 관련 기사", rendered)

    def test_related_article_markup_escapes_headline_publisher_and_url(self):
        row = dict(related_news=[dict(title='<script>alert("title")</script>', url="https://news.example.com/story?a=1&b=2",
                                     publisher="<신문>", published_at="2026-10-20"),
                                 dict(title="bad", url="javascript:alert(1)")])
        rendered = self.helpers["related_news_html"](row)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&lt;신문&gt;", rendered)
        self.assertIn("a=1&amp;b=2", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertIn('rel="noopener noreferrer"', rendered)

    def test_related_article_limit_applies_after_source_filter(self):
        assessed = self.assessed([youtube()] + [news(issue_title=f"'실낙원' 개봉 소식 {number}", source_url=f"https://news.example.com/article/{number}") for number in range(5)])
        ranked = self.helpers["build_core_issues"](assessed[assessed.source_group == YOUTUBE], news_pool=assessed)
        rendered = self.helpers["issue_evidence_html"](ranked.iloc[0])
        self.assertEqual(rendered.count("<li>"), 3)


if __name__ == "__main__":
    unittest.main()
