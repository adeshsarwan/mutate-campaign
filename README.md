# mutate-campaign

A guarded GitHub Actions control plane for Google Ads. ChatGPT writes reviewed JSON requests under `requests/`; GitHub Actions executes approved operations through the Google Ads API and stores the response as an Actions artifact.

## Safety defaults

- Writes are restricted to `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`.
- New campaigns are always created **PAUSED**.
- Campaign creation, budget changes, and status changes default to `validate_only: true`.
- Account currency must match `GOOGLE_ADS_EXPECTED_CURRENCY`.
- Daily budget is capped in account currency by `GOOGLE_ADS_MAX_DAILY_BUDGET`.
- App campaign creation requires explicit location and language targeting; accidental worldwide targeting is rejected.
- Campaign creation is sent as one atomic `GoogleAdsService.Mutate` request with `partial_failure=false`.
- Enabling a campaign is a separate explicit operation.
- No credentials belong in request JSON or repository files.

## Current environment

| Variable | Current value |
| --- | --- |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | `8902308627` |
| `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` | `8130408947` |
| `GOOGLE_ADS_EXPECTED_CURRENCY` | `IDR` |
| `GOOGLE_ADS_MAX_DAILY_BUDGET` | `300000` |

Required GitHub Environment secrets:

- `GOOGLE_ADS_DEVELOPER_TOKEN`
- `GOOGLE_ADS_CLIENT_ID`
- `GOOGLE_ADS_CLIENT_SECRET`
- `GOOGLE_ADS_REFRESH_TOKEN`

## Complete App campaign request

`create_app_campaign_draft` supports the campaign budget, Android App campaign, explicit country targeting, language targeting, selective conversion optimization, ad group, App ad text, and optional existing Google Ads image/video asset resource names.

Example:

```json
{
  "operation": "create_app_campaign_draft",
  "customer_id": "8130408947",
  "name": "Dot Connect | Tier 1 | Installs | Phase 1",
  "app_id": "com.gp.ds.flowdotconnectmania",
  "daily_budget_amount": 150000,
  "target_locations": [
    "United States",
    "United Kingdom",
    "Canada",
    "Australia",
    "New Zealand"
  ],
  "target_languages": ["English"],
  "optimization_goal_event": "first_open",
  "ad_group_name": "Dot Connect | Tier 1 | Install Creative 1",
  "headlines": [
    "Connect Every Dot",
    "Relax With Dot Connect"
  ],
  "descriptions": [
    "Connect matching dots and enjoy a relaxing puzzle challenge.",
    "Play quick, satisfying dot puzzles whenever you have a few minutes."
  ],
  "image_asset_resource_names": [],
  "youtube_video_asset_resource_names": [],
  "validate_only": true
}
```

The wrapper resolves `optimization_goal_event` to the matching imported Google Ads conversion action and places it in `campaign.selective_optimization`. A caller may instead supply `conversion_action_resource_name` explicitly.

If `target_cpa_amount` is omitted, Phase 1 uses `OPTIMIZE_INSTALLS_WITHOUT_TARGET_INSTALL_COST`. If supplied, it uses `OPTIMIZE_INSTALLS_TARGET_INSTALL_COST` and interprets the amount in the Google Ads account currency.

### Supported targeting

The guarded allowlist currently supports:

- United States
- United Kingdom
- Canada
- Australia
- New Zealand
- English

Additional locations/languages should be deliberately added to the allowlist rather than accepting arbitrary IDs from request JSON.

### Creative assets

Text creatives are created directly in the App ad. The wrapper accepts up to five headlines (30 characters each) and five descriptions (90 characters each).

Image and YouTube video fields currently accept **existing Google Ads Asset resource names**. Asset upload/creation is intentionally a separate future operation so uploaded media can be validated and reviewed before campaign creation.

A complete request can therefore be validated with text only while gameplay media is being prepared. When image/video assets exist in Google Ads, add their resource names and validate again before creation.

## Other supported operations

### Read accessible customers

```json
{"operation": "list_accessible_customers"}
```

### Account/measurement inspection

```json
{
  "operation": "measurement_inspection",
  "customer_id": "8130408947"
}
```

### Campaign report

```json
{
  "operation": "campaign_report",
  "customer_id": "8130408947",
  "days": 30
}
```

### Change daily budget

```json
{
  "operation": "update_campaign_daily_budget",
  "customer_id": "8130408947",
  "budget_resource_name": "customers/8130408947/campaignBudgets/123",
  "daily_budget_amount": 150000,
  "validate_only": true
}
```

### Pause or enable

```json
{
  "operation": "set_campaign_status",
  "customer_id": "8130408947",
  "campaign_resource_name": "customers/8130408947/campaigns/123",
  "status": "PAUSED",
  "validate_only": true
}
```

Use `ENABLED` only after campaign targeting, conversion goals, creatives, and budget have been reviewed.

## Execution flow

1. ChatGPT writes a uniquely named request under `requests/`.
2. Push to `main` triggers `.github/workflows/google-ads-request.yml`.
3. GitHub Actions authenticates with Google Ads using Environment secrets.
4. `src/main.py` enforces customer, currency, budget, targeting, and status guardrails.
5. The Google Ads API validates or executes the atomic mutation.
6. The JSON result is uploaded as an Actions artifact.

The workflow also supports manual `workflow_dispatch` for troubleshooting.
