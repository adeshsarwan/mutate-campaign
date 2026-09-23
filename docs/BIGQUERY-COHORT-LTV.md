# BigQuery cohort LTV reporting

The `cohort_ltv_report` operation combines Google Ads acquisition cost with Firebase/GA4 raw events exported to BigQuery.

## GitHub environment

The existing workflow uses the GitHub Environment `google.env`. Add the following values there.

### Secret

- `GCP_SERVICE_ACCOUNT_JSON` — complete JSON key for a dedicated Google Cloud service account used only for this reporting workflow.

### Variables

- `BIGQUERY_PROJECT_ID` — Google Cloud project containing the GA4 export.
- `BIGQUERY_DATASET` — GA4 dataset, normally named `analytics_<property_id>`.
- `BIGQUERY_LOCATION` — BigQuery dataset location, commonly `US`.
- `GA4_CAMPAIGN_NAME` — optional exact GA4/Firebase `traffic_source.name` for the paid campaign. If omitted, the Google Ads campaign name is used.
- `USD_TO_ADS_CURRENCY` — USD to Google Ads account currency conversion used to compare USD ad revenue with non-USD acquisition cost.

## Google Cloud permissions

Create a dedicated service account. Grant only what is required to execute read-only queries:

- BigQuery Job User on the query/billing project.
- BigQuery Data Viewer on the GA4 export dataset.

Do not commit the service-account key to the repository.

## Required Firebase / GA4 data

Link the Firebase/GA4 property to BigQuery and export Analytics events. The report expects GA4 export tables named `events_YYYYMMDD`.

It uses:
- `first_open` for acquisition cohorts.
- `traffic_source.name` on the user's first open for campaign attribution.
- `session_start` or `user_engagement` for retention.
- `ad_impression.event_value_in_usd` for cumulative ad LTV.

The report intentionally returns an attribution diagnostic before CAC/LTV is trusted. If no `first_open` users match the campaign name, inspect `top_first_open_attribution_rows` in the JSON artifact rather than treating all app users as paid users.

## Request

Example:

```json
{
  "operation": "cohort_ltv_report",
  "customer_id": "8130408947",
  "campaign_id": "24233136440",
  "start_date": "2026-09-09",
  "end_date": "2026-09-22",
  "usd_to_ads_currency": 17569.6
}
```

`bigquery_project`, `bigquery_dataset`, `bigquery_location`, and `ga4_campaign_name` can optionally be supplied in a request file; otherwise GitHub Environment variables are used.

## Output definitions

- Acquired user: unique `user_pseudo_id` whose `first_open` matches the configured GA4 campaign.
- D1/D3/D7/D14/D30 retention: user has `session_start` or `user_engagement` exactly N calendar days after acquisition.
- Current active: user active during the final seven calendar days of the requested report.
- LTV: cumulative `event_value_in_usd` from `ad_impression` for the cohort.
- CAC: Google Ads spend divided by GA4-attributed acquired users.
- Reconciliation CAC: Google Ads spend divided by Google Ads-reported installs.

Only cohorts old enough to reach a retention/LTV checkpoint are included in that checkpoint's aggregate denominator.
