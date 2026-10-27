# OTT service identity images

These small images identify the service links in subscription availability
results. They are public icons supplied by the services' own websites, not
generated substitutes. The names and marks belong to the respective services.

`sources.json` records the final official source URL, retrieval time, dimensions
and SHA-256 for each local PNG. Rendering embeds the local file as an image data
URL and does not request third-party image servers from the user's browser.

The bounded capture script is `scripts/fetch_ott_provider_logos.py`. It uses no
model, service login or account data. An unknown service is rendered as its
source-confirmed name until a verified identity image is added.
