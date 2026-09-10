import json
import os
import sys
from datetime import date, timedelta

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPE = "https://www.googleapis.com/auth/admob.report"


def required_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def d(v):
    y, m, day = map(int, v.split("-"))
    return {"year": y, "month": m, "day": day}


def money(micros):
    return float(micros or 0) / 1_000_000.0


def main():
    request_path, result_path = sys.argv[1], sys.argv[2]
    with open(request_path) as f:
        req = json.load(f)

    publisher_id = req.get("publisher_id") or required_env("ADMOB_PUBLISHER_ID")
    app_id = req.get("app_id") or os.getenv("ADMOB_APP_ID")
    end = req.get("end_date") or date.today().isoformat()
    start = req.get("start_date") or (date.today() - timedelta(days=7)).isoformat()

    creds = Credentials(
        token=None,
        refresh_token=required_env("ADMOB_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required_env("ADMOB_CLIENT_ID"),
        client_secret=required_env("ADMOB_CLIENT_SECRET"),
        scopes=[SCOPE],
    )
    service = build("admob", "v1", credentials=creds, cache_discovery=False)

    spec = {
        "dateRange": {"startDate": d(start), "endDate": d(end)},
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
            "estimated_earnings_usd": money(metrics.get("ESTIMATED_EARNINGS", {}).get("microsValue")),
            "impressions": int(metrics.get("IMPRESSIONS", {}).get("integerValue", 0)),
            "ad_requests": int(metrics.get("AD_REQUESTS", {}).get("integerValue", 0)),
            "matched_requests": int(metrics.get("MATCHED_REQUESTS", {}).get("integerValue", 0)),
            "impression_rpm_usd": money(metrics.get("IMPRESSION_RPM", {}).get("microsValue")),
        }
        out["match_rate"] = (out["matched_requests"] / out["ad_requests"]) if out["ad_requests"] else None
        rows.append(out)
        for k in ("impressions", "ad_requests", "matched_requests"):
            totals[k] += out[k]
        totals["estimated_earnings_usd"] += out["estimated_earnings_usd"]

    totals["match_rate"] = (totals["matched_requests"] / totals["ad_requests"]) if totals["ad_requests"] else None
    totals["ecpm_usd"] = (totals["estimated_earnings_usd"] * 1000 / totals["impressions"]) if totals["impressions"] else None
    result = {"operation": "admob_report", "publisher_id": publisher_id, "start_date": start, "end_date": end, "totals": totals, "rows": rows}
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    with open(result_path, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
