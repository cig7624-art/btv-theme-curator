# Public homepage source fixtures

These reduced fixtures preserve structures and factual names from official public homes captured by `Verify public OTT homes` on 2026-10-22. They are regression sources, not fictional showcase data. Request headers, identity responses, credentials and tracking are excluded.

- TVING: https://www.tving.com/ — homepage CollectionPage JSON-LD, same groups confirmed in rendered home.
- Netflix: https://www.netflix.com/kr/ — public landing page popularity strip (not a member home).
- Disney+: https://www.disneyplus.com/ko-kr — localized public banners, TOP 10 and collection links. The browser later redirected to the US; the fixture is the original localized HTTP document.
- Wavve: https://www.wavve.com/ — public DOM shelves and only the observed public v1/home + v1/catalog response fields required for matching those shelves. No private endpoint is requested.

The runner was outside Korea. Korean URL/language do not establish geographic equivalence. Exact capture evidence is in artifacts/ott_public_probe/summary.json.
