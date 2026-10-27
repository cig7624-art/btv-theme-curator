"""Brand images describe confirmed subscriptions without changing source offers."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ott_provider_branding import render_provider_logos_html, subscription_provider_cards


class ProviderBrandingTests(unittest.TestCase):
    def test_registered_images_match_their_recorded_official_public_sources(self):
        assets = Path(__file__).resolve().parents[1] / 'assets' / 'ott-providers'
        provenance = json.loads((assets / 'sources.json').read_text())
        expected = {'netflix', 'tving', 'wavve', 'watcha-play', 'coupang-play', 'disney', 'apple'}
        self.assertEqual(set(provenance['providers']), expected)
        self.assertFalse(provenance['credential_use'])
        for identifier, source in provenance['providers'].items():
            with self.subTest(provider=identifier):
                payload = (assets / (identifier + '.png')).read_bytes()
                self.assertTrue(payload.startswith(b'\x89PNG\r\n\x1a\n'))
                self.assertEqual(hashlib.sha256(payload).hexdigest(), source['sha256'])
                self.assertEqual(source['status'], 'downloaded')
                self.assertTrue(source['source_url'].startswith('https://'))
                card = {'provider_id': identifier, 'name': source['name'], 'offer_type': 'subscription'}
                markup = render_provider_logos_html([card])
                self.assertIn('<img', markup)
                self.assertIn('alt="' + source['name'] + '"', markup)
                self.assertIn('src="data:image/png;base64,', markup)

    def test_subscription_only_deduplicates_quality_and_aliases_without_mutating_source(self):
        result = {"status": "available", "providers": [
            {"provider_id": "wavve", "name": "웨이브", "offer_type": "rent", "url": "https://wavve.com/rent"},
            {"provider_id": "wavve", "name": "웨이브", "offer_type": "subscription", "url": "https://wavve.com/play"},
            {"provider_id": "wavve", "name": "웨이브", "offer_type": "subscription", "url": "https://wavve.com/hd"},
            {"provider_id": "disney-plus", "name": "Disney+", "offer_type": "subscription"},
            {"provider_id": "disney", "name": "디즈니+", "offer_type": "subscription", "url": "https://disneyplus.com/play"},
            {"provider_id": "apple", "name": "Apple TV", "offer_type": "buy", "url": "https://tv.apple.com/buy"},
            {"provider_id": "tving", "name": "티빙", "price": 0},
        ]}
        before = deepcopy(result)
        cards = subscription_provider_cards(result)
        self.assertEqual([row["provider_id"] for row in cards], ["wavve", "disney"])
        self.assertEqual(cards[0]["url"], "https://wavve.com/play")
        self.assertEqual(cards[1]["url"], "https://disneyplus.com/play")
        self.assertEqual(result, before)

    def test_unconfirmed_or_invalid_results_never_become_subscription_claims(self):
        row = {"provider_id": "netflix", "name": "넷플릭스", "offer_type": "subscription"}
        for result in (None, [], {}, {"status": "unknown", "providers": [row]},
                       {"status": "fetch_error", "providers": [row]}, {"status": "available", "providers": "bad"}):
            self.assertEqual(subscription_provider_cards(result), [])

    def test_known_logo_is_embedded_locally_and_accessible_without_a_network_request(self):
        rows = [{"provider_id": "tving", "name": "티빙", "offer_type": "subscription", "url": "https://tving.com/play"}]
        with patch('ott_provider_branding._logo_data_url', return_value='data:image/png;base64,iVBORw0KGgo='):
            markup = render_provider_logos_html(rows)
        self.assertIn('class="ott-title-service" data-provider-id="tving"', markup)
        self.assertIn('alt="티빙"', markup)
        self.assertIn('src="data:image/png;base64,', markup)
        self.assertIn('aria-label="티빙에서 구독으로 보기"', markup)
        self.assertNotIn('대여', markup)
        self.assertEqual(render_provider_logos_html([{**rows[0], "offer_type": "rent"}]), '')

    def test_unknown_source_names_are_escaped_and_cannot_borrow_registered_brand_images(self):
        rows = [{"provider_id": "unknown-source", "name": '<script>왓챠</script>', "offer_type": "subscription", "url": "javascript:alert(1)"},
                {"provider_id": "unknown-second", "name": "넷플릭스", "offer_type": "subscription"}]
        markup = render_provider_logos_html(rows)
        self.assertNotIn('<script>', markup)
        self.assertIn('&lt;script&gt;', markup)
        self.assertNotIn('<img', markup)
        self.assertNotIn('href=', markup)
        self.assertNotIn('javascript:', markup)


if __name__ == '__main__':
    unittest.main()
