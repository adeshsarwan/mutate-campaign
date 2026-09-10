import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app_campaign_report import (
    build_report as build_google_ads_report,
    clean_id,
    conversion_actions,
    get_customer_info,
    load_client,
    report_date_window,
)

ADMOB_SCOPE = "https://www.googleapis.com/auth/admob.report"


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def admob_date(value: str) -> dict:
    y, m, d = map(int, value.split("-"))
    return {"year": y, "month": m, "day": d}


def micros_to_money(value) -> float:
    return float(value or 0) / 1_000_000.0


def safe_div(numerator: float, denominator: float):
    return numerator / denominator if denominator else None


def build_admob_report(request: dict) -> dict:
    publisher_id = request.get("publisher_id") or required_env("ADMOB_PUBLISHER_ID")
    app_id = request.get("app_id") or os.getenv("ADMOB_APP_ID")
    end = request.get("end_date") or date.today().isoformat()
    start = request.get("start_date") or (date.today() - timedelta(days=7)).isoformat()

    creds = Credentials(
        token=None,
        refresh_token=required_env("ADMOB_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required_env("ADMOB_CLIENT_ID"),
        client_secret=required_env("ADMOB_CLIENT_SECRET"),
        scopes=[ADMOB_SCOPE],
    )
    service = build("admob", "v1", credentials=creds, cache_discovery=False)
    spec = {
        "dateRange": {"startDate": admob_date(start), "endDate": admob_date(end)},
        "dimensions": ["DATE", "APP"],
        "metrics": ["ESTIMATED_EARNINGS", "IMPRESSIONS", "AD_REQUESTS", "MATCHED_REQUESTS", "IMPRESSION_RPM"],
        "localizationSettings": {"currencyCode": "USD", "languageCode": "en-US"},
    }
    if app_id:
        spec["dimensionFilters"] = [{"dimension": "APP", "matchesAny": {"values": [app_id]}}]

    response = service.accounts().networkReport().generate(
        parent=f"accounts/{publisher_id}", body={"reportSpec": spec}
    ).execute()

    rows = []
    totals = {"estimated_earnings_usd": 0.0, "impressions": 0, "ad_requests": 0, "matched_requests": 0}
    for item in response:
        row = item.get("row")
        if not row:
            continue
        dims = row.get("dimensionValues", {})
        metrics = row.get("metricValues", {})
        out = {
            "date": dims.get("DATE", {}).get("value"),
            "app": dims.get("APP", {}).get("displayLabel") or dims.get("APP", {}).get("value"),
            "app_id": dims.get("APP", {}).get("value"),
            "estimated_earnings_usd": micros_to_money(metrics.get("ESTIMATED_EARNINGS", {}).get("microsValue")),
            "impressions": int(metrics.get("IMPRESSIONS", {}).get("integerValue", 0)),
            "ad_requests": int(metrics.get("AD_REQUESTS", {}).get("integerValue", 0)),
            "matched_requests": int(metrics.get("MATCHED_REQUESTS", {}).get("integerValue", 0)),
            "impression_rpm_usd": micros_to_money(metrics.get("IMPRESSION_RPM", {}).get("microsValue")),
        }
        out["match_rate"] = safe_div(out["matched_requests"], out["ad_requests"])
        rows.append(out)
        totals["estimated_earnings_usd"] += out["estimated_earnings_usd"]
        totals["impressions"] += out["impressions"]
        totals["ad_requests"] += out["ad_requests"]
        totals["matched_requests"] += out["matched_requests"]

    totals["match_rate"] = safe_div(totals["matched_requests"], totals["ad_requests"])
    totals["ecpm_usd"] = safe_div(totals["estimated_earnings_usd"] * 1000, totals["impressions"])
    return {"publisher_id": publisher_id, "app_id": app_id, "start_date": start, "end_date": end, "totals": totals, "rows": rows}


def is_ad_impression_action(action: dict) -> bool:
    searchable = " ".join([
        str(action.get("name") or ""),
        str(action.get("firebase_event_name") or ""),
        str(action.get("ga4_event_name") or ""),
    ]).lower()
    return "ad_impression" in searchable or "ad impression" in searchable


def configured_ad_impression_actions(client, customer_id: str) -> list[dict]:
    """Find configured actions independently of whether they have conversion rows.

    This fixes the previous bug where detection depended on campaign metric rows;
    Google Ads omits zero-volume conversion actions from segmented metric output.
    """
    actions = conversion_actions(client, clean_id(customer_id))
    return [action for action in actions.values() if is_ad_impression_action(action)]


def explicit_conversion_action_diagnostic(client, request: dict, actions: list[dict]) -> dict:
    cid = clean_id(request["customer_id"])
    info = get_customer_info(client, cid)
    date_clause, start_date, end_date = report_date_window(info, request)
    campaign_filter = ""
    if request.get("campaign_id"):
        campaign_filter = f" AND campaign.id = {clean_id(request['campaign_id'])}"

    service = client.get_service("GoogleAdsService")
    diagnostics = []

    for action in actions:
        resource_name = action["resource_name"]
        rows = service.search(customer_id=cid, query=f"""
            SELECT campaign.id,
                   campaign.name,
                   segments.date,
                   segments.conversion_action,
                   metrics.conversions,
                   metrics.conversions_value,
                   metrics.all_conversions,
                   metrics.all_conversions_value
            FROM campaign
            WHERE {date_clause}{campaign_filter}
              AND segments.conversion_action = '{resource_name}'
            ORDER BY segments.date
        """)

        daily = []
        totals = {
            "primary_conversions": 0.0,
            "primary_conversion_value": 0.0,
            "all_conversions": 0.0,
            "all_conversion_value": 0.0,
        }
        campaigns_seen = set()
        for row in rows:
            item = {
                "date": str(row.segments.date),
                "campaign_id": str(row.campaign.id),
                "campaign_name": row.campaign.name,
                "primary_conversions": float(row.metrics.conversions),
                "primary_conversion_value": float(row.metrics.conversions_value),
                "all_conversions": float(row.metrics.all_conversions),
                "all_conversion_value": float(row.metrics.all_conversions_value),
            }
            daily.append(item)
            campaigns_seen.add(str(row.campaign.id))
            for key in totals:
                totals[key] += item[key]

        diagnostics.append({
            "action": action,
            "date_range": {"start_date": start_date, "end_date": end_date},
            "campaign_filter": clean_id(request["campaign_id"]) if request.get("campaign_id") else None,
            "campaigns_seen": sorted(campaigns_seen),
            "metric_rows_found": len(daily),
            "has_attributed_conversions": totals["all_conversions"] > 0,
            "has_attributed_value": totals["all_conversion_value"] != 0,
            "totals": totals,
            "daily": daily,
            "value_semantics": {
                "conversion_action_default_currency": action.get("default_currency_code"),
                "conversion_action_default_value": action.get("default_value"),
                "always_use_default_value": action.get("always_use_default_value"),
                "customer_currency": info["currency"],
                "dynamic_event_value_expected": not bool(action.get("always_use_default_value")),
            },
        })

    duplicate_warning = len(actions) > 1
    return {
        "configured_action_count": len(actions),
        "duplicate_action_warning": duplicate_warning,
        "duplicate_status": "POTENTIAL_DUPLICATE" if duplicate_warning else "CLEAN",
        "actions": diagnostics,
    }


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python src/combined_roi_report.py REQUEST_JSON RESULT_JSON")
    request_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    request = json.loads(request_path.read_text())

    client = load_client()
    ads_report = build_google_ads_report(client, request)
    admob_report = build_admob_report(request)

    configured_actions = configured_ad_impression_actions(client, request["customer_id"])
    diagnostic = explicit_conversion_action_diagnostic(client, request, configured_actions)

    ad_impression_conversions = sum(
        action["totals"]["all_conversions"] for action in diagnostic["actions"]
    )
    ad_impression_value = sum(
        action["totals"]["all_conversion_value"] for action in diagnostic["actions"]
    )
    primary_ad_impression_conversions = sum(
        action["totals"]["primary_conversions"] for action in diagnostic["actions"]
    )
    primary_ad_impression_value = sum(
        action["totals"]["primary_conversion_value"] for action in diagnostic["actions"]
    )

    fx = float(request.get("usd_to_ads_currency") or 0)
    ads_totals = ads_report["totals"]
    admob_totals = admob_report["totals"]
    revenue_usd = float(admob_totals.get("estimated_earnings_usd") or 0)
    spend = float(ads_totals.get("cost_amount") or 0)
    installs = float(ads_totals.get("installs") or 0)
    revenue_ads_currency = revenue_usd * fx if fx else None

    result = {
        "ok": True,
        "operation": "combined_roi_report",
        "date_range": {"start_date": ads_report["start_date"], "end_date": ads_report["end_date"]},
        "google_ads": ads_report,
        "admob": admob_report,
        "firebase_ad_impression_path": {
            "configured": bool(configured_actions),
            "configured_action_count": len(configured_actions),
            "flowing_into_google_ads": ad_impression_conversions > 0,
            "value_flowing_into_google_ads": ad_impression_value != 0,
            "all_conversions": ad_impression_conversions,
            "all_conversion_value": ad_impression_value,
            "primary_conversions": primary_ad_impression_conversions,
            "primary_conversion_value": primary_ad_impression_value,
            "duplicate_action_warning": diagnostic["duplicate_action_warning"],
            "diagnostic": diagnostic,
            "interpretation": (
                "ad_impression conversions and monetary value are attributed in Google Ads"
                if ad_impression_conversions > 0 and ad_impression_value != 0
                else "ad_impression conversions are attributed, but monetary value is zero"
                if ad_impression_conversions > 0
                else "ad_impression is configured, but no campaign-attributed conversion rows were reported in this date range"
                if configured_actions
                else "no configured ad_impression conversion action was found"
            ),
        },
        "roi": {
            "spend_amount": spend,
            "spend_currency": ads_report["currency"],
            "installs": installs,
            "cost_per_install": safe_div(spend, installs),
            "admob_revenue_usd": revenue_usd,
            "admob_revenue_per_install_usd": safe_div(revenue_usd, installs),
            "usd_to_ads_currency": fx or None,
            "admob_revenue_in_ads_currency": revenue_ads_currency,
            "blended_roas": safe_div(revenue_ads_currency, spend) if revenue_ads_currency is not None else None,
            "blended_roi_percent": ((revenue_ads_currency - spend) / spend * 100) if revenue_ads_currency is not None and spend else None,
            "google_ads_ad_impression_conversion_value": ad_impression_value,
            "google_ads_value_roas": safe_div(ad_impression_value, spend),
            "notes": [
                "blended_roas uses total AdMob app revenue for the date range, not campaign-attributed AdMob revenue",
                "google_ads_value_roas uses ad_impression conversion value explicitly attributed by Google Ads",
                "configured ad_impression actions are discovered independently of metric rows so zero-volume actions are not falsely reported as missing",
            ],
        },
    }

    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
