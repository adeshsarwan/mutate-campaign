import json
import sys
from pathlib import Path

from app_campaign_report import (
    clean_id,
    conversion_actions,
    get_customer_info,
    is_install_action,
    load_client,
    report_date_window,
    safe_div,
)


AGE_LABELS = {
    "AGE_RANGE_18_24": "18-24",
    "AGE_RANGE_25_34": "25-34",
    "AGE_RANGE_35_44": "35-44",
    "AGE_RANGE_45_54": "45-54",
    "AGE_RANGE_55_64": "55-64",
    "AGE_RANGE_65_UP": "65+",
    "AGE_RANGE_UNDETERMINED": "Unknown",
    "UNKNOWN": "Unknown",
    "UNSPECIFIED": "Unknown",
}


def age_key(row) -> str:
    raw = row.ad_group_criterion.age_range.type_.name
    return AGE_LABELS.get(raw, raw)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python src/campaign_demographic_report.py REQUEST_JSON RESULT_JSON")

    request_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    request = json.loads(request_path.read_text())

    client = load_client()
    cid = clean_id(request["customer_id"])
    info = get_customer_info(client, cid)
    date_clause, start_date, end_date = report_date_window(info, request)

    campaign_filter = ""
    if request.get("campaign_id"):
        campaign_filter = f" AND campaign.id = {clean_id(request['campaign_id'])}"

    service = client.get_service("GoogleAdsService")

    rows = service.search(customer_id=cid, query=f"""
        SELECT
          campaign.id,
          campaign.name,
          ad_group_criterion.age_range.type,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value,
          metrics.all_conversions,
          metrics.all_conversions_value
        FROM age_range_view
        WHERE {date_clause}{campaign_filter}
    """)

    by_age = {}
    campaign_name = None
    for row in rows:
        label = age_key(row)
        campaign_name = row.campaign.name
        item = by_age.setdefault(label, {
            "age_range": label,
            "impressions": 0,
            "clicks": 0,
            "cost_amount": 0.0,
            "primary_conversions": 0.0,
            "primary_conversion_value": 0.0,
            "all_conversions": 0.0,
            "all_conversion_value": 0.0,
            "installs": 0.0,
            "install_value": 0.0,
        })
        item["impressions"] += int(row.metrics.impressions)
        item["clicks"] += int(row.metrics.clicks)
        item["cost_amount"] += float(row.metrics.cost_micros) / 1_000_000.0
        item["primary_conversions"] += float(row.metrics.conversions)
        item["primary_conversion_value"] += float(row.metrics.conversions_value)
        item["all_conversions"] += float(row.metrics.all_conversions)
        item["all_conversion_value"] += float(row.metrics.all_conversions_value)

    actions = conversion_actions(client, cid)
    install_resources = {
        resource_name for resource_name, action in actions.items()
        if is_install_action(action)
    }

    conversion_rows = service.search(customer_id=cid, query=f"""
        SELECT
          campaign.id,
          ad_group_criterion.age_range.type,
          segments.conversion_action,
          metrics.all_conversions,
          metrics.all_conversions_value
        FROM age_range_view
        WHERE {date_clause}{campaign_filter}
          AND segments.conversion_action IS NOT NULL
    """)

    for row in conversion_rows:
        resource = row.segments.conversion_action
        if resource not in install_resources:
            continue
        label = age_key(row)
        item = by_age.setdefault(label, {
            "age_range": label,
            "impressions": 0,
            "clicks": 0,
            "cost_amount": 0.0,
            "primary_conversions": 0.0,
            "primary_conversion_value": 0.0,
            "all_conversions": 0.0,
            "all_conversion_value": 0.0,
            "installs": 0.0,
            "install_value": 0.0,
        })
        item["installs"] += float(row.metrics.all_conversions)
        item["install_value"] += float(row.metrics.all_conversions_value)

    preferred_order = ["18-24", "25-34", "35-44", "45-54", "55-64", "65+", "Unknown"]
    order_index = {label: i for i, label in enumerate(preferred_order)}

    output_rows = []
    for item in by_age.values():
        item["ctr"] = safe_div(item["clicks"], item["impressions"])
        item["cost_per_install"] = safe_div(item["cost_amount"], item["installs"])
        output_rows.append(item)

    output_rows.sort(key=lambda x: order_index.get(x["age_range"], 999))

    totals = {
        "impressions": sum(x["impressions"] for x in output_rows),
        "clicks": sum(x["clicks"] for x in output_rows),
        "cost_amount": sum(x["cost_amount"] for x in output_rows),
        "installs": sum(x["installs"] for x in output_rows),
    }
    totals["ctr"] = safe_div(totals["clicks"], totals["impressions"])
    totals["cost_per_install"] = safe_div(totals["cost_amount"], totals["installs"])

    result = {
        "ok": True,
        "operation": "campaign_demographic_report",
        "customer_id": cid,
        "customer_name": info["name"],
        "campaign_id": clean_id(request["campaign_id"]) if request.get("campaign_id") else None,
        "campaign_name": campaign_name,
        "start_date": start_date,
        "end_date": end_date,
        "currency": info["currency"],
        "time_zone": info["time_zone"],
        "notes": [
            "Age metrics are reported by Google Ads age_range_view and aggregated across ad groups.",
            "Unknown should be retained when evaluating demographics because Google may not know every user's age.",
            "Installs are summed only from conversion actions classified as install/download actions."
        ],
        "totals": totals,
        "age_ranges": output_rows,
    }

    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
