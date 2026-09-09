# Media requests

JSON files in this directory control repository-media operations. Creating or changing a `*.json` file here triggers `.github/workflows/google-ads-media.yml`.

Use `dry_run: true` before every new media selection. Paths must remain under `media/`; absolute paths and `..` traversal are rejected. Google Ads writes are restricted to `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`.
