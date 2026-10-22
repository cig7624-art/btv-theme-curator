# Public home verification

`summary.json` records the public official homepage probe, source URL, capture environment and access result. It contains no account session or authentication headers.

Detailed DOM/API captures and screenshots are short-lived artifacts of `Verify public OTT homes` when `capture_evidence` is selected. They are not committed as production data. Reduced regression sources are in `tests/fixtures/ott/`; application observations are in `ott_home_snapshot.json`.

The live collector separately records its newest timestamp in the snapshot. A previous detailed probe summary does not replace that date. The image sample check is in `artifacts/ott_asset_validation.json`.
