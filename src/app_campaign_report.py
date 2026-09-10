import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google.ads.googleads.client import GoogleAdsClient


def clean_id(value: str | int) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def load_client() -> GoogleAdsClient:
    required = [
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN",
    ]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError(f"Missing secrets: {', '.join(missing)}")

    config: dict[str, Any] = {
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "use_proto_plus": True,
    }
    login_customer_id = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
    if login_customer_id:
        config["login_customer_id"] = clean_id(login_customer_id)
    return GoogleAdsClient.load_from_dict(config)


def get_customer_info(client: GoogleAdsClient, customer_id: str) -> dict[str, Any]:
    cid = clean_id(customer_id)
    rows = list(client.get_service("GoogleAdsService").search(customer_id=cid, query="""
        SELECT customer.id, customer.descriptive_name, customer.currency_code,
               customer.time_zone
        FROM customer
        LIMIT 1
    """))
    if not rows:
        raise RuntimeError(f"Unable to read customer {cid}")
    customer = rows[0].customer
    return {
        "id": str(customer.id),
        "name": customer.descriptive_name,
        "currency": customer.currency_code,
        "time_zone": customer.time_zone,
    }


def report_date_window(customer_info: dict[str, Any], request: dict[str, Any], default_days: int = 30) -> tuple[str, str, str]:
    tz_name = str(customer_info.get("time_zone") or "UTC")
    try:
        today = datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        today = datetime.now(ZoneInfo("UTC")).date()

    if request.get("start_date") or request.get("end_date"):
        if not request.get("start_date") or not request.get("end_date"):
            raise RuntimeError("start_date and end_date must be provided together in YYYY-MM-DD format.")
        start = str(request["start_date"])
        end = str(request["end_date"])
        try:
            start_date = datetime.strptime(start, "%Y-%m-%d").date()
            end_date = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError as exc:
            raise RuntimeError("start_date and end_date must use YYYY-MM-DD format.") from exc
        if start_date > end_date:
            raise RuntimeError("start_date must be on or before end_date.")
    else:
        days = int(request.get("days", default_days))
        if days < 1 or days > 3650:
            raise RuntimeError("days must be between 1 and 3650.")
        end_date = today
        start_date = today - timedelta(days=days - 1)
        start = start_date.isoformat()
        end = end_date.isoformat()

    return f"segments.date BETWEEN '{start}' AND '{end}'", start, end


def conversion_actions(client: GoogleAdsClient, cid: str) -> dict[str, dict[str, Any]]:
    rows = client.get_service("GoogleAdsService").search(customer_id=cid, query="""
        SELECT conversion_action.resource_name,
               conversion_action.id,
               conversion_action.name,
               conversion_action.status,
               conversion_action.type,
               conversion_action.category,
               conversion_action.origin,
               conversion_action.primary_for_goal,
               conversion_action.value_settings.default_value,
               conversion_action.value_settings.default_currency_code,
               conversion_action.value_settings.always_use_default_value,
               conversion_action.firebase_settings.event_name,
               conversion_action.google_analytics_4_settings.event_name
        FROM conversion_action
    """)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        action = row.conversion_action
        out[action.resource_name] = {
            "id": str(action.id),
            "resource_name": action.resource_name,
            "name": action.name,
            "status": action.status.name,
            "type": action.type_.name,
            "category": action.category.name,
            "origin": action.origin.name,
            "primary_for_goal": bool(action.primary_for_goal),
            "firebase_event_name": action.firebase_settings.event_name,
            "ga4_event_name": action.google_analytics_4_settings.event_name,
            "default_value": float(action.value_settings.default_value),
            "default_currency_code": action.value_settings.default_currency_code,
            "always_use_default_value": bool(action.value_settings.always_use_default_value),
        }
    return out


def is_install_action(action: dict[str, Any]) -> bool:
    action_type = str(action.get("type") or "").upper()
    category = str(action.get("category") or "").upper()
    name = str(action.get("name") or "").lower()
    return (
        category == "DOWNLOAD"
        or "INSTALL" in action_type
        or "INSTALL" in name.upper()
        or name in {"first_open", "first open"}
    )


def safe_div(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return numerator / denominator


def build_report(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = clean_id(request["customer_id"])
    info = get_customer_info(client, cid)
    currency = info["currency"]
    date_clause, start_date, end_date = report_date_window(info, request)
    campaign_filter = ""
    if request.get("campaign_id"):
        campaign_filter = f" AND campaign.id = {clean_id(request['campaign_id'])}"

    service = client.get_service("GoogleAdsService")
    summary_rows = service.search(customer_id=cid, query=f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.advertising_channel_type,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value,
               metrics.all_conversions, metrics.all_conversions_value
        FROM campaign
        WHERE {date_clause}{campaign_filter}
        ORDER BY metrics.cost_micros DESC
    """)

    campaigns: dict[str, dict[str, Any]] = {}
    for row in summary_rows:
        campaign_id = str(row.campaign.id)
        cost = row.metrics.cost_micros / 1_000_000
        clicks = int(row.metrics.clicks)
        impressions = int(row.metrics.impressions)
        primary_conversions = float(row.metrics.conversions)
        all_conversions = float(row.metrics.all_conversions)
        campaigns[campaign_id] = {
            "campaign_id": campaign_id,
            "name": row.campaign.name,
            "status": row.campaign.status.name,
            "channel": row.campaign.advertising_channel_type.name,
            "impressions": impressions,
            "clicks": clicks,
            "ctr": safe_div(clicks, impressions),
            "cost_amount": cost,
            "currency": currency,
            "primary_conversions": primary_conversions,
            "primary_conversion_value": float(row.metrics.conversions_value),
            "all_conversions": all_conversions,
            "all_conversion_value": float(row.metrics.all_conversions_value),
            "installs": 0.0,
            "install_value": 0.0,
            "cost_per_install": None,
            "conversion_actions": [],
        }

    actions = conversion_actions(client, cid)
    breakdown_rows = service.search(customer_id=cid, query=f"""
        SELECT campaign.id, campaign.name,
               segments.conversion_action,
               metrics.conversions, metrics.conversions_value,
               metrics.all_conversions, metrics.all_conversions_value
        FROM campaign
        WHERE {date_clause}{campaign_filter}
          AND segments.conversion_action IS NOT NULL
    """)

    for row in breakdown_rows:
        campaign_id = str(row.campaign.id)
        if campaign_id not in campaigns:
            continue
        action_resource = row.segments.conversion_action
        action = actions.get(action_resource, {
            "resource_name": action_resource,
            "name": action_resource,
            "status": "UNKNOWN",
            "type": "UNKNOWN",
            "category": "UNKNOWN",
            "origin": "UNKNOWN",
            "primary_for_goal": False,
            "firebase_event_name": "",
            "ga4_event_name": "",
            "default_value": 0.0,
            "default_currency_code": "",
            "always_use_default_value": False,
        })
        all_conversions = float(row.metrics.all_conversions)
        all_value = float(row.metrics.all_conversions_value)
        install_action = is_install_action(action)
        item = {
            **action,
            "is_install": install_action,
            "primary_conversions": float(row.metrics.conversions),
            "primary_conversion_value": float(row.metrics.conversions_value),
            "all_conversions": all_conversions,
            "all_conversion_value": all_value,
        }
        campaigns[campaign_id]["conversion_actions"].append(item)
        if install_action:
            campaigns[campaign_id]["installs"] += all_conversions
            campaigns[campaign_id]["install_value"] += all_value

    totals = {
        "impressions": 0,
        "clicks": 0,
        "cost_amount": 0.0,
        "primary_conversions": 0.0,
        "primary_conversion_value": 0.0,
        "all_conversions": 0.0,
        "all_conversion_value": 0.0,
        "installs": 0.0,
        "install_value": 0.0,
        "cost_per_install": None,
        "currency": currency,
    }

    for campaign in campaigns.values():
        campaign["conversion_actions"].sort(
            key=lambda item: (float(item["all_conversions"]), float(item["all_conversion_value"])),
            reverse=True,
        )
        campaign["cost_per_install"] = safe_div(campaign["cost_amount"], campaign["installs"])
        totals["impressions"] += campaign["impressions"]
        totals["clicks"] += campaign["clicks"]
        totals["cost_amount"] += campaign["cost_amount"]
        totals["primary_conversions"] += campaign["primary_conversions"]
        totals["primary_conversion_value"] += campaign["primary_conversion_value"]
        totals["all_conversions"] += campaign["all_conversions"]
        totals["all_conversion_value"] += campaign["all_conversion_value"]
        totals["installs"] += campaign["installs"]
        totals["install_value"] += campaign["install_value"]

    totals["cost_per_install"] = safe_div(totals["cost_amount"], totals["installs"])
    totals["ctr"] = safe_div(totals["clicks"], totals["impressions"])

    return {
        "customer_id": cid,
        "customer_name": info["name"],
        "start_date": start_date,
        "end_date": end_date,
        "time_zone": info["time_zone"],
        "currency": currency,
        "reporting_semantics": {
            "primary_conversions": "Google Ads metrics.conversions. These are primary/biddable conversions only.",
            "all_conversions": "Google Ads metrics.all_conversions. Includes secondary conversion actions.",
            "installs": "Sum of All conversions from conversion actions classified as app installs/downloads.",
            "cost_per_install": "Campaign cost divided by installs from All conversions.",
        },
        "totals": totals,
        "campaigns": list(campaigns.values()),
    }


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python src/app_campaign_report.py REQUEST_JSON RESULT_JSON")
    request_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    request = json.loads(request_path.read_text())
    client = load_client()
    try:
        result = {"ok": True, "operation": request.get("operation"), "result": build_report(client, request)}
    except Exception as exc:
        result = {"ok": False, "operation": request.get("operation"), "error": str(exc)}
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, indent=2))
        raise
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
