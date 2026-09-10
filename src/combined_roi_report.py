import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app_campaign_report import build_report as build_google_ads_report, load_client

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


def find_ad_impression_actions(ads_report: dict) -> list[dict]:
    found = []
    for campaign in ads_report.get("campaigns", []):
        for action in campaign.get("conversion_actions", []):
            searchable = " ".join([
                str(action.get("name") or ""),
                str(action.get("firebase_event_name") or ""),
                str(action.get("ga4_event_name") or ""),
            ]).lower()
            if "ad_impression" in searchable or "ad impression" in searchable:
                found.append({
                    "campaign_id": campaign.get("campaign_id"),
                    "campaign_name": campaign.get("name"),
                    **action,
                })
    return found


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python src/combined_roi_report.py REQUEST_JSON RESULT_JSON")
    request_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    request = json.loads(request_path.read_text())

    client = load_client()
    ads_report = build_google_ads_report(client, request)
    admob_report = build_admob_report(request)

    fx = float(request.get("usd_to_ads_currency") or 0)
    ads_totals = ads_report["totals"]
    admob_totals = admob_report["totals"]
    ad_impression_actions = find_ad_impression_actions(ads_report)

    ad_impression_conversions = sum(float(x.get("all_conversions") or 0) for x in ad_impression_actions)
    ad_impression_value = sum(float(x.get("all_conversion_value") or 0) for x in ad_impression_actions)
    primary_ad_impression_conversions = sum(float(x.get("primary_conversions") or 0) for x in ad_impression_actions)
    primary_ad_impression_value = sum(float(x.get("primary_conversion_value") or 0) for x in ad_impression_actions)

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
            "detected": bool(ad_impression_actions),
            "flowing_into_google_ads": ad_impression_conversions > 0,
            "all_conversions": ad_impression_conversions,
            "all_conversion_value": ad_impression_value,
            "primary_conversions": primary_ad_impression_conversions,
            "primary_conversion_value": primary_ad_impression_value,
            "actions": ad_impression_actions,
            "interpretation": (
                "ad_impression conversion data is present in Google Ads reporting"
                if ad_impression_conversions > 0
                else "an ad_impression action may exist, but no attributed conversions were reported in this date range"
                if ad_impression_actions
                else "no ad_impression conversion action was found in Google Ads reporting"
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
                "google_ads_value_roas uses the ad_impression conversion value attributed by Google Ads when available",
            ],
        },
    }

    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
