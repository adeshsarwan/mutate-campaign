import json
import os
import sys
from pathlib import Path
from typing import Any

from google.ads.googleads.client import GoogleAdsClient
from google.api_core import protobuf_helpers


def clean_id(value: str | int) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def load_client() -> GoogleAdsClient:
    required = [
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN",
    ]
    missing = [k for k in required if not os.environ.get(k)]
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


def allowed_customers() -> set[str]:
    raw = os.environ.get("GOOGLE_ADS_ALLOWED_CUSTOMER_IDS", "")
    return {clean_id(x) for x in raw.split(",") if clean_id(x)}


def assert_allowed(customer_id: str) -> str:
    cid = clean_id(customer_id)
    allowed = allowed_customers()
    if not allowed:
        raise RuntimeError("GOOGLE_ADS_ALLOWED_CUSTOMER_IDS is empty; writes are disabled.")
    if cid not in allowed:
        raise RuntimeError(f"Customer {cid} is not in GOOGLE_ADS_ALLOWED_CUSTOMER_IDS")
    return cid


def usd_to_micros(value: float) -> int:
    return int(round(float(value) * 1_000_000))


def check_budget_limit(daily_budget_usd: float) -> None:
    limit = float(os.environ.get("GOOGLE_ADS_MAX_DAILY_BUDGET_USD", "20"))
    if float(daily_budget_usd) <= 0 or float(daily_budget_usd) > limit:
        raise RuntimeError(f"daily_budget_usd must be > 0 and <= {limit}")


def list_accessible_customers(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    service = client.get_service("CustomerService")
    response = service.list_accessible_customers()
    return {"customers": [x.replace("customers/", "") for x in response.resource_names]}


def campaign_report(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = clean_id(request["customer_id"])
    days = int(request.get("days", 30))
    query = f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.status,
          campaign.advertising_channel_type,
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value,
          metrics.all_conversions,
          metrics.all_conversions_value
        FROM campaign
        WHERE segments.date DURING LAST_{days}_DAYS
        ORDER BY metrics.cost_micros DESC
    """
    rows = client.get_service("GoogleAdsService").search(customer_id=cid, query=query)
    out = []
    for row in rows:
        out.append({
            "campaign_id": str(row.campaign.id),
            "name": row.campaign.name,
            "status": row.campaign.status.name,
            "channel": row.campaign.advertising_channel_type.name,
            "cost_usd": row.metrics.cost_micros / 1_000_000,
            "conversions": float(row.metrics.conversions),
            "conversion_value": float(row.metrics.conversions_value),
            "all_conversions": float(row.metrics.all_conversions),
            "all_conversion_value": float(row.metrics.all_conversions_value),
        })
    return {"customer_id": cid, "days": days, "campaigns": out}


def create_app_campaign_draft(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = assert_allowed(request["customer_id"])
    daily_budget_usd = float(request["daily_budget_usd"])
    check_budget_limit(daily_budget_usd)
    validate_only = bool(request.get("validate_only", True))

    budget_service = client.get_service("CampaignBudgetService")
    budget_op = client.get_type("CampaignBudgetOperation")
    budget = budget_op.create
    budget.name = f"{request['name']} budget"
    budget.amount_micros = usd_to_micros(daily_budget_usd)
    budget.explicitly_shared = False
    budget_delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.delivery_method = budget_delivery_method

    budget_response = budget_service.mutate_campaign_budgets(
        customer_id=cid,
        operations=[budget_op],
        validate_only=validate_only,
    )

    if validate_only:
        return {
            "validated": True,
            "created": False,
            "operation": "create_app_campaign_draft",
            "customer_id": cid,
            "name": request["name"],
            "app_id": request["app_id"],
            "daily_budget_usd": daily_budget_usd,
            "status": "PAUSED",
            "note": "Budget validated. Re-submit with validate_only=false to create resources.",
        }

    budget_resource = budget_response.results[0].resource_name

    campaign_service = client.get_service("CampaignService")
    campaign_op = client.get_type("CampaignOperation")
    campaign = campaign_op.create
    campaign.name = request["name"]
    campaign.status = client.enums.CampaignStatusEnum.PAUSED
    campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.MULTI_CHANNEL
    campaign.advertising_channel_sub_type = client.enums.AdvertisingChannelSubTypeEnum.APP_CAMPAIGN
    campaign.campaign_budget = budget_resource
    campaign.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    campaign.app_campaign_setting.app_id = request["app_id"]
    campaign.app_campaign_setting.app_store = client.enums.AppCampaignAppStoreEnum.GOOGLE_APP_STORE

    target_cpa_usd = request.get("target_cpa_usd")
    if target_cpa_usd is not None:
        campaign.app_campaign_setting.bidding_strategy_goal_type = (
            client.enums.AppCampaignBiddingStrategyGoalTypeEnum.OPTIMIZE_INSTALLS_TARGET_INSTALL_COST
        )
        campaign.target_cpa.target_cpa_micros = usd_to_micros(float(target_cpa_usd))
    else:
        campaign.app_campaign_setting.bidding_strategy_goal_type = (
            client.enums.AppCampaignBiddingStrategyGoalTypeEnum.OPTIMIZE_INSTALLS_WITHOUT_TARGET_INSTALL_COST
        )
        campaign.maximize_conversions = client.get_type("MaximizeConversions")

    response = campaign_service.mutate_campaigns(customer_id=cid, operations=[campaign_op])
    return {
        "validated": False,
        "created": True,
        "customer_id": cid,
        "campaign_resource_name": response.results[0].resource_name,
        "budget_resource_name": budget_resource,
        "status": "PAUSED",
    }


def update_campaign_daily_budget(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = assert_allowed(request["customer_id"])
    daily_budget_usd = float(request["daily_budget_usd"])
    check_budget_limit(daily_budget_usd)
    validate_only = bool(request.get("validate_only", True))

    service = client.get_service("CampaignBudgetService")
    op = client.get_type("CampaignBudgetOperation")
    op.update.resource_name = request["budget_resource_name"]
    op.update.amount_micros = usd_to_micros(daily_budget_usd)
    op.update_mask.CopyFrom(protobuf_helpers.field_mask(None, op.update._pb))
    service.mutate_campaign_budgets(customer_id=cid, operations=[op], validate_only=validate_only)
    return {"validated": validate_only, "daily_budget_usd": daily_budget_usd}


def set_campaign_status(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = assert_allowed(request["customer_id"])
    status = str(request["status"]).upper()
    if status not in {"PAUSED", "ENABLED"}:
        raise RuntimeError("status must be PAUSED or ENABLED")
    validate_only = bool(request.get("validate_only", True))

    service = client.get_service("CampaignService")
    op = client.get_type("CampaignOperation")
    op.update.resource_name = request["campaign_resource_name"]
    op.update.status = getattr(client.enums.CampaignStatusEnum, status)
    op.update_mask.CopyFrom(protobuf_helpers.field_mask(None, op.update._pb))
    service.mutate_campaigns(customer_id=cid, operations=[op], validate_only=validate_only)
    return {"validated": validate_only, "status": status}


OPERATIONS = {
    "list_accessible_customers": list_accessible_customers,
    "campaign_report": campaign_report,
    "create_app_campaign_draft": create_app_campaign_draft,
    "update_campaign_daily_budget": update_campaign_daily_budget,
    "set_campaign_status": set_campaign_status,
}


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python src/main.py REQUEST_JSON RESULT_JSON")
    request_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    request = json.loads(request_path.read_text())
    operation = request.get("operation")
    if operation not in OPERATIONS:
        raise RuntimeError(f"Unsupported operation: {operation}")

    client = load_client()
    try:
        result = {"ok": True, "operation": operation, "result": OPERATIONS[operation](client, request)}
    except Exception as exc:
        result = {"ok": False, "operation": operation, "error": str(exc)}
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, indent=2))
        raise

    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
