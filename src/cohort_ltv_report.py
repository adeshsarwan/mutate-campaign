import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from google.cloud import bigquery

from app_campaign_report import clean_id, conversion_actions, get_customer_info, is_install_action, load_client


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def safe_div(numerator: float, denominator: float):
    return numerator / denominator if denominator else None


def validate_project(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,126}", value):
        raise RuntimeError(f"Invalid BigQuery project id: {value}")
    return value


def validate_dataset(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise RuntimeError(f"Invalid BigQuery dataset id: {value}")
    return value


def parse_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise RuntimeError(f"Invalid date {value}; expected YYYY-MM-DD") from exc


def bigquery_client(project_id: str) -> bigquery.Client:
    # Authentication is provided by google-github-actions/auth via
    # Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS).
    return bigquery.Client(project=project_id)


def campaign_breakdown(client: bigquery.Client, table_prefix: str, start: str, end: str, location: str) -> list[dict[str, Any]]:
    sql = f"""
    WITH first_opens AS (
      SELECT
        user_pseudo_id,
        ARRAY_AGG(
          STRUCT(
            PARSE_DATE('%Y%m%d', event_date) AS acquisition_date,
            traffic_source.name AS campaign_name,
            traffic_source.source AS source,
            traffic_source.medium AS medium
          )
          ORDER BY event_timestamp
          LIMIT 1
        )[OFFSET(0)] AS first_open
      FROM `{table_prefix}events_*`
      WHERE _TABLE_SUFFIX BETWEEN @start_suffix AND @end_suffix
        AND event_name = 'first_open'
        AND user_pseudo_id IS NOT NULL
      GROUP BY user_pseudo_id
    )
    SELECT
      COALESCE(first_open.campaign_name, '(null)') AS campaign_name,
      COALESCE(first_open.source, '(null)') AS source,
      COALESCE(first_open.medium, '(null)') AS medium,
      COUNT(*) AS users
    FROM first_opens
    GROUP BY campaign_name, source, medium
    ORDER BY users DESC
    LIMIT 50
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_suffix", "STRING", start.replace("-", "")),
            bigquery.ScalarQueryParameter("end_suffix", "STRING", end.replace("-", "")),
        ]
    )
    return [dict(row.items()) for row in client.query(sql, job_config=cfg, location=location).result()]


def revenue_diagnostic(client: bigquery.Client, table_prefix: str, start: str, end: str, location: str) -> dict[str, Any]:
    sql = f"""
    SELECT
      COUNT(*) AS ad_impression_events,
      COUNTIF(event_value_in_usd IS NOT NULL AND event_value_in_usd != 0) AS top_level_value_events,
      SUM(COALESCE(event_value_in_usd, 0)) AS top_level_event_value_usd,
      COUNTIF((SELECT ep.value.int_value FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1) IS NOT NULL) AS param_int_value_events,
      COUNTIF((SELECT ep.value.double_value FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1) IS NOT NULL) AS param_double_value_events,
      COUNTIF((SELECT ep.value.float_value FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1) IS NOT NULL) AS param_float_value_events,
      SUM(COALESCE(CAST((SELECT ep.value.int_value FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1) AS FLOAT64), 0)) AS param_int_value_raw_sum,
      SUM(COALESCE((SELECT ep.value.double_value FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1), 0)) AS param_double_value_sum,
      SUM(COALESCE((SELECT ep.value.float_value FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1), 0)) AS param_float_value_sum,
      SUM(
        COALESCE(NULLIF(event_value_in_usd, 0),
          CASE
            WHEN UPPER(COALESCE((SELECT ep.value.string_value FROM UNNEST(event_params) ep WHERE ep.key = 'currency' LIMIT 1), 'USD')) = 'USD'
            THEN COALESCE(
              (SELECT ep.value.double_value FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1),
              (SELECT ep.value.float_value FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1),
              SAFE_DIVIDE(CAST((SELECT ep.value.int_value FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1) AS FLOAT64), 1000000.0)
            )
            ELSE NULL
          END,
          0
        )
      ) AS normalized_revenue_usd,
      ARRAY_AGG(DISTINCT COALESCE(
        (SELECT ep.value.string_value FROM UNNEST(event_params) ep WHERE ep.key = 'currency' LIMIT 1),
        '(null)'
      ) IGNORE NULLS LIMIT 20) AS currencies
    FROM `{table_prefix}events_*`
    WHERE _TABLE_SUFFIX BETWEEN @start_suffix AND @end_suffix
      AND event_name = 'ad_impression'
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_suffix", "STRING", start.replace("-", "")),
            bigquery.ScalarQueryParameter("end_suffix", "STRING", end.replace("-", "")),
        ]
    )
    rows = list(client.query(sql, job_config=cfg, location=location).result())
    return dict(rows[0].items()) if rows else {}


def cohort_rows(
    client: bigquery.Client,
    table_prefix: str,
    start: str,
    end: str,
    campaign_name: str,
    location: str,
) -> list[dict[str, Any]]:
    sql = f"""
    WITH base AS (
      SELECT
        PARSE_DATE('%Y%m%d', event_date) AS event_date,
        event_timestamp,
        user_pseudo_id,
        event_name,
        traffic_source.name AS acquisition_campaign,
        event_value_in_usd,
        (SELECT ep.value.int_value
         FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1) AS param_value_int,
        (SELECT ep.value.double_value
         FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1) AS param_value_double,
        (SELECT ep.value.float_value
         FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1) AS param_value_float,
        (SELECT ep.value.string_value
         FROM UNNEST(event_params) ep WHERE ep.key = 'currency' LIMIT 1) AS param_currency,
        COALESCE(
          NULLIF(event_value_in_usd, 0),
          CASE
            WHEN UPPER(COALESCE((SELECT ep.value.string_value FROM UNNEST(event_params) ep WHERE ep.key = 'currency' LIMIT 1), 'USD')) = 'USD'
            THEN COALESCE(
              (SELECT ep.value.double_value FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1),
              (SELECT ep.value.float_value FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1),
              SAFE_DIVIDE(CAST((SELECT ep.value.int_value FROM UNNEST(event_params) ep WHERE ep.key = 'value' LIMIT 1) AS FLOAT64), 1000000.0)
            )
            ELSE NULL
          END,
          0
        ) AS ad_revenue_value_usd
      FROM `{table_prefix}events_*`
      WHERE _TABLE_SUFFIX BETWEEN @start_suffix AND @end_suffix
        AND user_pseudo_id IS NOT NULL
    ),
    first_opens AS (
      SELECT
        user_pseudo_id,
        ARRAY_AGG(
          STRUCT(event_date AS acquisition_date, acquisition_campaign AS campaign_name)
          ORDER BY event_timestamp
          LIMIT 1
        )[OFFSET(0)] AS first_open
      FROM base
      WHERE event_name = 'first_open'
      GROUP BY user_pseudo_id
    ),
    acquired AS (
      SELECT
        user_pseudo_id,
        first_open.acquisition_date AS acquisition_date,
        first_open.campaign_name AS campaign_name
      FROM first_opens
      WHERE LOWER(TRIM(COALESCE(first_open.campaign_name, ''))) = LOWER(TRIM(@campaign_name))
    ),
    activity AS (
      SELECT DISTINCT user_pseudo_id, event_date
      FROM base
      WHERE event_name IN ('session_start', 'user_engagement')
    ),
    retention_by_user AS (
      SELECT
        a.user_pseudo_id,
        a.acquisition_date,
        MAX(IF(DATE_DIFF(act.event_date, a.acquisition_date, DAY) = 1, 1, 0)) AS d1,
        MAX(IF(DATE_DIFF(act.event_date, a.acquisition_date, DAY) = 3, 1, 0)) AS d3,
        MAX(IF(DATE_DIFF(act.event_date, a.acquisition_date, DAY) = 7, 1, 0)) AS d7,
        MAX(IF(DATE_DIFF(act.event_date, a.acquisition_date, DAY) = 14, 1, 0)) AS d14,
        MAX(IF(DATE_DIFF(act.event_date, a.acquisition_date, DAY) = 30, 1, 0)) AS d30,
        MAX(IF(act.event_date BETWEEN DATE_SUB(@end_date, INTERVAL 6 DAY) AND @end_date, 1, 0)) AS active_last_7d
      FROM acquired a
      LEFT JOIN activity act
        ON act.user_pseudo_id = a.user_pseudo_id
       AND act.event_date BETWEEN a.acquisition_date AND @end_date
      GROUP BY a.user_pseudo_id, a.acquisition_date
    ),
    revenue_by_user AS (
      SELECT
        a.user_pseudo_id,
        a.acquisition_date,
        SUM(IF(b.event_name = 'ad_impression' AND b.event_date BETWEEN a.acquisition_date AND @end_date,
               COALESCE(b.ad_revenue_value_usd, 0), 0)) AS revenue_total_usd,
        SUM(IF(b.event_name = 'ad_impression' AND DATE_DIFF(b.event_date, a.acquisition_date, DAY) BETWEEN 0 AND 0,
               COALESCE(b.ad_revenue_value_usd, 0), 0)) AS revenue_d0_usd,
        SUM(IF(b.event_name = 'ad_impression' AND DATE_DIFF(b.event_date, a.acquisition_date, DAY) BETWEEN 0 AND 1,
               COALESCE(b.ad_revenue_value_usd, 0), 0)) AS revenue_d1_usd,
        SUM(IF(b.event_name = 'ad_impression' AND DATE_DIFF(b.event_date, a.acquisition_date, DAY) BETWEEN 0 AND 3,
               COALESCE(b.ad_revenue_value_usd, 0), 0)) AS revenue_d3_usd,
        SUM(IF(b.event_name = 'ad_impression' AND DATE_DIFF(b.event_date, a.acquisition_date, DAY) BETWEEN 0 AND 7,
               COALESCE(b.ad_revenue_value_usd, 0), 0)) AS revenue_d7_usd,
        SUM(IF(b.event_name = 'ad_impression' AND DATE_DIFF(b.event_date, a.acquisition_date, DAY) BETWEEN 0 AND 14,
               COALESCE(b.ad_revenue_value_usd, 0), 0)) AS revenue_d14_usd,
        SUM(IF(b.event_name = 'ad_impression' AND DATE_DIFF(b.event_date, a.acquisition_date, DAY) BETWEEN 0 AND 30,
               COALESCE(b.ad_revenue_value_usd, 0), 0)) AS revenue_d30_usd
      FROM acquired a
      LEFT JOIN base b
        ON b.user_pseudo_id = a.user_pseudo_id
       AND b.event_date BETWEEN a.acquisition_date AND @end_date
      GROUP BY a.user_pseudo_id, a.acquisition_date
    )
    SELECT
      r.acquisition_date,
      COUNT(*) AS acquired_users,
      SUM(r.d1) AS d1_retained,
      SUM(r.d3) AS d3_retained,
      SUM(r.d7) AS d7_retained,
      SUM(r.d14) AS d14_retained,
      SUM(r.d30) AS d30_retained,
      SUM(r.active_last_7d) AS active_last_7d,
      SUM(v.revenue_total_usd) AS cumulative_revenue_usd,
      SUM(v.revenue_d0_usd) AS revenue_d0_usd,
      SUM(v.revenue_d1_usd) AS revenue_d1_usd,
      SUM(v.revenue_d3_usd) AS revenue_d3_usd,
      SUM(v.revenue_d7_usd) AS revenue_d7_usd,
      SUM(v.revenue_d14_usd) AS revenue_d14_usd,
      SUM(v.revenue_d30_usd) AS revenue_d30_usd
    FROM retention_by_user r
    JOIN revenue_by_user v USING (user_pseudo_id, acquisition_date)
    GROUP BY r.acquisition_date
    ORDER BY r.acquisition_date
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_suffix", "STRING", start.replace("-", "")),
            bigquery.ScalarQueryParameter("end_suffix", "STRING", end.replace("-", "")),
            bigquery.ScalarQueryParameter("end_date", "DATE", end),
            bigquery.ScalarQueryParameter("campaign_name", "STRING", campaign_name),
        ]
    )
    out = []
    for row in client.query(sql, job_config=cfg, location=location).result():
        item = dict(row.items())
        item["acquisition_date"] = item["acquisition_date"].isoformat()
        for key, value in list(item.items()):
            if hasattr(value, "__float__") and key.endswith("_usd"):
                item[key] = float(value or 0)
        out.append(item)
    return out


def google_ads_daily(request: dict[str, Any]) -> dict[str, Any]:
    client = load_client()
    cid = clean_id(request["customer_id"])
    info = get_customer_info(client, cid)
    start = parse_date(request["start_date"])
    end = parse_date(request["end_date"])
    campaign_id = clean_id(request["campaign_id"])
    service = client.get_service("GoogleAdsService")

    spend_rows = service.search(customer_id=cid, query=f"""
      SELECT segments.date, campaign.id, campaign.name,
             metrics.impressions, metrics.clicks, metrics.cost_micros
      FROM campaign
      WHERE segments.date BETWEEN '{start}' AND '{end}'
        AND campaign.id = {campaign_id}
      ORDER BY segments.date
    """)

    daily: dict[str, dict[str, Any]] = {}
    campaign_name = None
    for row in spend_rows:
        day = str(row.segments.date)
        campaign_name = row.campaign.name
        daily[day] = {
            "date": day,
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "impressions": int(row.metrics.impressions),
            "clicks": int(row.metrics.clicks),
            "spend_amount": float(row.metrics.cost_micros) / 1_000_000.0,
            "ads_reported_installs": 0.0,
        }

    actions = conversion_actions(client, cid)
    install_rows = service.search(customer_id=cid, query=f"""
      SELECT segments.date, segments.conversion_action,
             metrics.all_conversions
      FROM campaign
      WHERE segments.date BETWEEN '{start}' AND '{end}'
        AND campaign.id = {campaign_id}
        AND segments.conversion_action IS NOT NULL
    """)

    for row in install_rows:
        action = actions.get(row.segments.conversion_action)
        if not action or not is_install_action(action):
            continue
        day = str(row.segments.date)
        if day not in daily:
            daily[day] = {
                "date": day,
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "impressions": 0,
                "clicks": 0,
                "spend_amount": 0.0,
                "ads_reported_installs": 0.0,
            }
        daily[day]["ads_reported_installs"] += float(row.metrics.all_conversions)

    return {
        "customer_id": cid,
        "customer_name": info["name"],
        "currency": info["currency"],
        "time_zone": info["time_zone"],
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "daily": [daily[k] for k in sorted(daily)],
    }


def enrich_cohorts(
    cohorts: list[dict[str, Any]],
    ads: dict[str, Any],
    end: str,
    usd_to_ads_currency: float | None,
) -> list[dict[str, Any]]:
    ads_by_day = {row["date"]: row for row in ads["daily"]}
    end_dt = datetime.strptime(end, "%Y-%m-%d").date()

    for row in cohorts:
        users = int(row["acquired_users"] or 0)
        acq_dt = datetime.strptime(row["acquisition_date"], "%Y-%m-%d").date()
        age = (end_dt - acq_dt).days
        ads_day = ads_by_day.get(row["acquisition_date"], {})
        spend = float(ads_day.get("spend_amount") or 0)
        ads_installs = float(ads_day.get("ads_reported_installs") or 0)
        revenue = float(row.get("cumulative_revenue_usd") or 0)

        row["cohort_age_days"] = age
        row["google_ads_spend_amount"] = spend
        row["google_ads_currency"] = ads["currency"]
        row["ads_reported_installs"] = ads_installs
        row["cac_per_ga4_user_ads_currency"] = safe_div(spend, users)
        row["cac_per_ads_install_ads_currency"] = safe_div(spend, ads_installs)
        row["ltv_per_ga4_user_usd"] = safe_div(revenue, users)

        for d in (1, 3, 7, 14, 30):
            eligible = age >= d
            row[f"d{d}_eligible"] = eligible
            row[f"d{d}_retention_rate"] = safe_div(float(row.get(f"d{d}_retained") or 0), users) if eligible else None
            if eligible:
                row[f"d{d}_ltv_per_user_usd"] = safe_div(float(row.get(f"revenue_d{d}_usd") or 0), users)
            else:
                row[f"d{d}_ltv_per_user_usd"] = None

        if ads["currency"] == "USD":
            cac_usd = safe_div(spend, users)
        elif usd_to_ads_currency:
            cac_usd = safe_div(spend, users)
            cac_usd = safe_div(cac_usd, usd_to_ads_currency) if cac_usd is not None else None
        else:
            cac_usd = None
        row["cac_per_ga4_user_usd"] = cac_usd
        row["ltv_cac_ratio"] = safe_div(row["ltv_per_ga4_user_usd"], cac_usd) if cac_usd is not None else None

    return cohorts


def summarize(cohorts: list[dict[str, Any]], ads: dict[str, Any], usd_to_ads_currency: float | None) -> dict[str, Any]:
    users = sum(int(r["acquired_users"] or 0) for r in cohorts)
    active = sum(int(r["active_last_7d"] or 0) for r in cohorts)
    revenue = sum(float(r["cumulative_revenue_usd"] or 0) for r in cohorts)
    spend = sum(float(r["google_ads_spend_amount"] or 0) for r in cohorts)
    ads_installs = sum(float(r["ads_reported_installs"] or 0) for r in cohorts)

    if ads["currency"] == "USD":
        spend_usd = spend
    elif usd_to_ads_currency:
        spend_usd = spend / usd_to_ads_currency
    else:
        spend_usd = None

    summary = {
        "ga4_attributed_acquired_users": users,
        "active_users_last_7d": active,
        "active_last_7d_rate": safe_div(active, users),
        "cumulative_ad_revenue_usd": revenue,
        "ltv_per_ga4_user_usd": safe_div(revenue, users),
        "google_ads_spend_amount": spend,
        "google_ads_currency": ads["currency"],
        "google_ads_spend_usd": spend_usd,
        "ads_reported_installs": ads_installs,
        "cac_per_ga4_user_ads_currency": safe_div(spend, users),
        "cac_per_ads_install_ads_currency": safe_div(spend, ads_installs),
        "cac_per_ga4_user_usd": safe_div(spend_usd, users) if spend_usd is not None else None,
    }
    summary["ltv_cac_ratio"] = safe_div(summary["ltv_per_ga4_user_usd"], summary["cac_per_ga4_user_usd"]) if summary["cac_per_ga4_user_usd"] is not None else None

    for d in (1, 3, 7, 14, 30):
        eligible_users = sum(int(r["acquired_users"] or 0) for r in cohorts if r[f"d{d}_eligible"])
        retained = sum(int(r.get(f"d{d}_retained") or 0) for r in cohorts if r[f"d{d}_eligible"])
        checkpoint_revenue = sum(float(r.get(f"revenue_d{d}_usd") or 0) for r in cohorts if r[f"d{d}_eligible"])
        summary[f"d{d}_eligible_users"] = eligible_users
        summary[f"d{d}_retained_users"] = retained
        summary[f"d{d}_retention_rate"] = safe_div(retained, eligible_users)
        summary[f"d{d}_ltv_per_user_usd"] = safe_div(checkpoint_revenue, eligible_users)

    return summary


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python src/cohort_ltv_report.py REQUEST_JSON RESULT_JSON")

    request = json.loads(Path(sys.argv[1]).read_text())
    result_path = Path(sys.argv[2])

    start = parse_date(request["start_date"])
    end = parse_date(request["end_date"])
    if start > end:
        raise RuntimeError("start_date must be on or before end_date")

    project_id = validate_project(request.get("bigquery_project") or required_env("BIGQUERY_PROJECT_ID"))
    dataset_id = validate_dataset(request.get("bigquery_dataset") or required_env("BIGQUERY_DATASET"))
    location = request.get("bigquery_location") or os.getenv("BIGQUERY_LOCATION") or "US"
    table_prefix = f"{project_id}.{dataset_id}."

    ads = google_ads_daily(request)
    campaign_name = (
        request.get("ga4_campaign_name")
        or os.getenv("GA4_CAMPAIGN_NAME")
        or ads.get("campaign_name")
    )
    if not campaign_name:
        raise RuntimeError("Unable to determine GA4 campaign name. Set GA4_CAMPAIGN_NAME or ga4_campaign_name.")

    bq = bigquery_client(project_id)
    breakdown = campaign_breakdown(bq, table_prefix, start, end, location)
    revenue_diag = revenue_diagnostic(bq, table_prefix, start, end, location)
    cohorts = cohort_rows(bq, table_prefix, start, end, campaign_name, location)

    fx_raw = request.get("usd_to_ads_currency") or os.getenv("USD_TO_ADS_CURRENCY")
    fx = float(fx_raw) if fx_raw else None
    cohorts = enrich_cohorts(cohorts, ads, end, fx)
    summary = summarize(cohorts, ads, fx)

    matched_users = summary["ga4_attributed_acquired_users"]
    result = {
        "ok": True,
        "operation": "cohort_ltv_report",
        "date_range": {"start_date": start, "end_date": end},
        "campaign": {
            "google_ads_customer_id": ads["customer_id"],
            "google_ads_campaign_id": ads["campaign_id"],
            "google_ads_campaign_name": ads["campaign_name"],
            "ga4_campaign_name_filter": campaign_name,
        },
        "bigquery": {
            "project_id": project_id,
            "dataset_id": dataset_id,
            "location": location,
            "source_table_pattern": "events_*",
        },
        "revenue_diagnostic": revenue_diag,
        "attribution_diagnostic": {
            "matched_campaign_users": matched_users,
            "top_first_open_attribution_rows": breakdown,
            "warning": (
                None
                if matched_users > 0
                else "No first_open users matched the GA4 campaign name. Inspect the attribution rows before using CAC/LTV."
            ),
        },
        "definitions": {
            "acquired_user": "A GA4/Firebase user_pseudo_id whose first_open is attributed to the configured GA4 traffic_source.name.",
            "retained_dN": "An acquired user with session_start or user_engagement exactly N days after first_open.",
            "active_last_7d": "An acquired user with session_start or user_engagement during the final 7 calendar days of the report.",
            "ltv": "Cumulative ad_impression revenue for the acquired cohort. Top-level event_value_in_usd is used when present; otherwise USD event_params.value is normalized by storage type (INT64 treated as micros, FLOAT/DOUBLE treated as currency units).",
            "cac": "Google Ads spend divided by GA4-attributed acquired users; Ads-reported install CAC is also included for reconciliation.",
        },
        "fx": {
            "usd_to_ads_currency": fx,
            "note": "Required for USD CAC and LTV:CAC when the Google Ads account currency is not USD.",
        },
        "summary": summary,
        "cohorts": cohorts,
        "google_ads_daily": ads["daily"],
    }

    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
