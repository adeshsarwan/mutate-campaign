# mutate-campaign

A guarded GitHub Actions control plane for Google Ads. ChatGPT can create a JSON request in `requests/`; GitHub Actions executes the approved operation through the Google Ads API and stores the response as a workflow artifact.

## Safety defaults

- Google Ads writes are restricted to `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`.
- New campaigns are always created **PAUSED**.
- `create_app_campaign_draft`, budget changes and status changes default to `validate_only: true`.
- Daily budget is capped by `GOOGLE_ADS_MAX_DAILY_BUDGET_USD`.
- Enabling a campaign is a separate explicit operation.
- No credentials belong in request JSON or repository files.

## Initial account configuration

For the current test account, set these GitHub Actions **repository variables**:

| Variable | Value |
| --- | --- |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | `8902308627` |
| `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` | `8130408947` |
| `GOOGLE_ADS_MAX_DAILY_BUDGET_USD` | `20` |

Add these as GitHub Actions **repository secrets**:

- `GOOGLE_ADS_DEVELOPER_TOKEN`
- `GOOGLE_ADS_CLIENT_ID`
- `GOOGLE_ADS_CLIENT_SECRET`
- `GOOGLE_ADS_REFRESH_TOKEN`

Do not commit these values.

## Supported operations

### Read accessible customers

```json
{
  "operation": "list_accessible_customers"
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

### Validate a new App campaign

```json
{
  "operation": "create_app_campaign_draft",
  "customer_id": "8130408947",
  "name": "Dot Connect - Install Test",
  "app_id": "YOUR_ANDROID_PACKAGE_NAME",
  "daily_budget_usd": 10,
  "validate_only": true
}
```

If `target_cpa_usd` is omitted, the App campaign uses Google's install-volume goal without a target install cost. If supplied, the campaign uses target install cost bidding.

After validation succeeds, create the campaign by submitting a new request with `validate_only: false`. The created campaign remains PAUSED.

### Change daily budget

```json
{
  "operation": "update_campaign_daily_budget",
  "customer_id": "8130408947",
  "budget_resource_name": "customers/8130408947/campaignBudgets/123",
  "daily_budget_usd": 15,
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

Use `ENABLED` only after campaign configuration, assets, conversion goals and budget have been reviewed.

## How ChatGPT uses it

1. ChatGPT writes a uniquely named JSON request under `requests/`.
2. A push to `main` triggers `.github/workflows/google-ads-request.yml`.
3. The Action authenticates to Google Ads using GitHub Secrets.
4. `src/main.py` validates the allowlist and budget guardrails and executes the operation.
5. The JSON result is uploaded as a GitHub Actions artifact for review.

The workflow also supports manual `workflow_dispatch` for troubleshooting.

## Current scope

This first version intentionally handles account discovery/reporting, creation of a paused Android App campaign, budget changes, and campaign pause/enable. Creative asset groups, image upload, YouTube video association, conversion-goal inspection, tROAS migration, and richer reporting should be added after authentication is verified against the test account.
