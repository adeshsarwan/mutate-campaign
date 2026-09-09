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


def amount_to_micros(value: float) -> int:
    return int(round(float(value) * 1_000_000))


def get_customer_info(client: GoogleAdsClient, customer_id: str) -> dict[str, Any]:
    cid = clean_id(customer_id)
    rows = list(client.get_service("GoogleAdsService").search(customer_id=cid, query="""
        SELECT customer.id, customer.descriptive_name, customer.currency_code,
               customer.time_zone, customer.manager, customer.test_account
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
        "manager": bool(customer.manager),
        "test_account": bool(customer.test_account),
    }


def write_currency_guard(client: GoogleAdsClient, customer_id: str) -> tuple[str, float]:
    info = get_customer_info(client, customer_id)
    actual = str(info["currency"]).upper()
    expected = os.environ.get("GOOGLE_ADS_EXPECTED_CURRENCY", "").strip().upper()
    if not expected:
        raise RuntimeError(
            "Writes are disabled until GOOGLE_ADS_EXPECTED_CURRENCY is configured to the account currency."
        )
    if actual != expected:
        raise RuntimeError(f"Currency safety block: account currency is {actual}, configured currency is {expected}.")

    limit_raw = os.environ.get("GOOGLE_ADS_MAX_DAILY_BUDGET", "").strip()
    if not limit_raw:
        legacy = os.environ.get("GOOGLE_ADS_MAX_DAILY_BUDGET_USD", "").strip()
        if actual == "USD" and legacy:
            limit_raw = legacy
        else:
            raise RuntimeError(
                "Writes are disabled until GOOGLE_ADS_MAX_DAILY_BUDGET is configured in the account currency."
            )
    return actual, float(limit_raw)


def request_budget_amount(request: dict[str, Any], currency: str) -> float:
    if "daily_budget_amount" in request:
        return float(request["daily_budget_amount"])
    if "daily_budget_usd" in request and currency == "USD":
        return float(request["daily_budget_usd"])
    raise RuntimeError(
        f"Use daily_budget_amount expressed in the Google Ads account currency ({currency})."
    )


def check_budget_limit(amount: float, limit: float, currency: str) -> None:
    if amount <= 0 or amount > limit:
        raise RuntimeError(f"daily_budget_amount must be > 0 and <= {limit} {currency}")


def list_accessible_customers(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    service = client.get_service("CustomerService")
    response = service.list_accessible_customers()
    return {"customers": [x.replace("customers/", "") for x in response.resource_names]}


def conversion_action_rows(client: GoogleAdsClient, cid: str) -> list[dict[str, Any]]:
    rows = client.get_service("GoogleAdsService").search(customer_id=cid, query="""
        SELECT conversion_action.id, conversion_action.name, conversion_action.status,
               conversion_action.type, conversion_action.category, conversion_action.origin,
               conversion_action.primary_for_goal,
               conversion_action.value_settings.default_value,
               conversion_action.value_settings.default_currency_code,
               conversion_action.value_settings.always_use_default_value,
               conversion_action.firebase_settings.event_name,
               conversion_action.firebase_settings.project_id,
               conversion_action.firebase_settings.property_id,
               conversion_action.firebase_settings.property_name,
               conversion_action.google_analytics_4_settings.event_name,
               conversion_action.google_analytics_4_settings.property_id,
               conversion_action.google_analytics_4_settings.property_name
        FROM conversion_action
        ORDER BY conversion_action.id
    """)
    out = []
    for row in rows:
        ca = row.conversion_action
        out.append({
            "id": str(ca.id),
            "name": ca.name,
            "status": ca.status.name,
            "type": ca.type_.name,
            "category": ca.category.name,
            "origin": ca.origin.name,
            "primary_for_goal": bool(ca.primary_for_goal),
            "value_settings": {
                "default_value": float(ca.value_settings.default_value),
                "default_currency_code": ca.value_settings.default_currency_code,
                "always_use_default_value": bool(ca.value_settings.always_use_default_value),
            },
            "firebase": {
                "event_name": ca.firebase_settings.event_name,
                "project_id": ca.firebase_settings.project_id,
                "property_id": str(ca.firebase_settings.property_id) if ca.firebase_settings.property_id else "",
                "property_name": ca.firebase_settings.property_name,
            },
            "ga4": {
                "event_name": ca.google_analytics_4_settings.event_name,
                "property_id": str(ca.google_analytics_4_settings.property_id) if ca.google_analytics_4_settings.property_id else "",
                "property_name": ca.google_analytics_4_settings.property_name,
            },
        })
    return out


def customer_goal_rows(client: GoogleAdsClient, cid: str) -> list[dict[str, Any]]:
    rows = client.get_service("GoogleAdsService").search(customer_id=cid, query="""
        SELECT customer_conversion_goal.category,
               customer_conversion_goal.origin,
               customer_conversion_goal.biddable,
               customer_conversion_goal.resource_name
        FROM customer_conversion_goal
        ORDER BY customer_conversion_goal.category, customer_conversion_goal.origin
    """)
    return [{
        "category": row.customer_conversion_goal.category.name,
        "origin": row.customer_conversion_goal.origin.name,
        "biddable": bool(row.customer_conversion_goal.biddable),
        "resource_name": row.customer_conversion_goal.resource_name,
    } for row in rows]


def event_match(action: dict[str, Any], wanted: str) -> bool:
    wanted = wanted.lower()
    candidates = [
        action.get("name", ""),
        action.get("firebase", {}).get("event_name", ""),
        action.get("ga4", {}).get("event_name", ""),
    ]
    return any(str(x).strip().lower() == wanted for x in candidates if x)


def measurement_inspection(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = clean_id(request["customer_id"])
    info = get_customer_info(client, cid)
    actions = conversion_action_rows(client, cid)
    goals = customer_goal_rows(client, cid)
    first_open = [a for a in actions if event_match(a, "first_open") or event_match(a, "First open")]
    ad_impression = [a for a in actions if event_match(a, "ad_impression")]

    return {
        "customer": info,
        "conversion_actions": actions,
        "customer_conversion_goals": goals,
        "readiness": {
            "first_open_found": bool(first_open),
            "first_open_primary": any(a["primary_for_goal"] for a in first_open),
            "ad_impression_found": bool(ad_impression),
            "ad_impression_primary": any(a["primary_for_goal"] for a in ad_impression),
            "ad_impression_uses_event_value": any(
                not a["value_settings"]["always_use_default_value"] for a in ad_impression
            ),
            "ad_impression_actions": ad_impression,
        },
    }


def account_preflight(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = clean_id(request["customer_id"])
    return {
        "customer": get_customer_info(client, cid),
        "conversion_actions": conversion_action_rows(client, cid),
    }


def campaign_report(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = clean_id(request["customer_id"])
    days = int(request.get("days", 30))
    currency = get_customer_info(client, cid)["currency"]
    query = f"""
        SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type,
               metrics.cost_micros, metrics.conversions, metrics.conversions_value,
               metrics.all_conversions, metrics.all_conversions_value
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
            "cost_amount": row.metrics.cost_micros / 1_000_000,
            "currency": currency,
            "conversions": float(row.metrics.conversions),
            "conversion_value": float(row.metrics.conversions_value),
            "all_conversions": float(row.metrics.all_conversions),
            "all_conversion_value": float(row.metrics.all_conversions_value),
        })
    return {"customer_id": cid, "days": days, "currency": currency, "campaigns": out}


def create_app_campaign_draft(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = assert_allowed(request["customer_id"])
    currency, limit = write_currency_guard(client, cid)
    daily_budget_amount = request_budget_amount(request, currency)
    check_budget_limit(daily_budget_amount, limit, currency)
    validate_only = bool(request.get("validate_only", True))

    budget_service = client.get_service("CampaignBudgetService")
    campaign_service = client.get_service("CampaignService")
    google_ads_service = client.get_service("GoogleAdsService")

    budget_resource = budget_service.campaign_budget_path(cid, -1)
    campaign_resource = campaign_service.campaign_path(cid, -2)

    budget_mutate = client.get_type("MutateOperation")
    budget_op = budget_mutate.campaign_budget_operation
    budget = budget_op.create
    budget.resource_name = budget_resource
    budget.name = f"{request['name']} budget"
    budget.amount_micros = amount_to_micros(daily_budget_amount)
    budget.explicitly_shared = False
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD

    campaign_mutate = client.get_type("MutateOperation")
    campaign_op = campaign_mutate.campaign_operation
    campaign = campaign_op.create
    campaign.resource_name = campaign_resource
    campaign.name = request["name"]
    campaign.status = client.enums.CampaignStatusEnum.PAUSED
    campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.MULTI_CHANNEL
    campaign.advertising_channel_sub_type = client.enums.AdvertisingChannelSubTypeEnum.APP_CAMPAIGN
    campaign.campaign_budget = budget_resource
    campaign.contains_eu_political_advertising = client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    campaign.app_campaign_setting.app_id = request["app_id"]
    campaign.app_campaign_setting.app_store = client.enums.AppCampaignAppStoreEnum.GOOGLE_APP_STORE

    if request.get("target_cpa_amount") is not None:
        campaign.app_campaign_setting.bidding_strategy_goal_type = client.enums.AppCampaignBiddingStrategyGoalTypeEnum.OPTIMIZE_INSTALLS_TARGET_INSTALL_COST
        campaign.target_cpa.target_cpa_micros = amount_to_micros(float(request["target_cpa_amount"]))
    elif request.get("target_cpa_usd") is not None:
        if currency != "USD":
            raise RuntimeError(f"target_cpa_usd is invalid for a {currency} account; use target_cpa_amount.")
        campaign.app_campaign_setting.bidding_strategy_goal_type = client.enums.AppCampaignBiddingStrategyGoalTypeEnum.OPTIMIZE_INSTALLS_TARGET_INSTALL_COST
        campaign.target_cpa.target_cpa_micros = amount_to_micros(float(request["target_cpa_usd"]))
    else:
        campaign.app_campaign_setting.bidding_strategy_goal_type = client.enums.AppCampaignBiddingStrategyGoalTypeEnum.OPTIMIZE_INSTALLS_WITHOUT_TARGET_INSTALL_COST
        campaign.maximize_conversions = client.get_type("MaximizeConversions")

    mutate_request = client.get_type("MutateGoogleAdsRequest")
    mutate_request.customer_id = cid
    mutate_request.mutate_operations.extend([budget_mutate, campaign_mutate])
    mutate_request.partial_failure = False
    mutate_request.validate_only = validate_only
    response = google_ads_service.mutate(request=mutate_request)

    if validate_only:
        return {
            "validated": True,
            "created": False,
            "operation": "create_app_campaign_draft",
            "customer_id": cid,
            "name": request["name"],
            "app_id": request["app_id"],
            "daily_budget_amount": daily_budget_amount,
            "currency": currency,
            "status": "PAUSED",
            "note": "Budget and campaign core validated atomically. No Google Ads resources were created.",
        }

    created_budget = response.mutate_operation_responses[0].campaign_budget_result.resource_name
    created_campaign = response.mutate_operation_responses[1].campaign_result.resource_name
    return {
        "validated": False,
        "created": True,
        "customer_id": cid,
        "currency": currency,
        "campaign_resource_name": created_campaign,
        "budget_resource_name": created_budget,
        "status": "PAUSED",
    }


def update_campaign_daily_budget(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = assert_allowed(request["customer_id"])
    currency, limit = write_currency_guard(client, cid)
    amount = request_budget_amount(request, currency)
    check_budget_limit(amount, limit, currency)
    validate_only = bool(request.get("validate_only", True))
    service = client.get_service("CampaignBudgetService")
    op = client.get_type("CampaignBudgetOperation")
    op.update.resource_name = request["budget_resource_name"]
    op.update.amount_micros = amount_to_micros(amount)
    op.update_mask.CopyFrom(protobuf_helpers.field_mask(None, op.update._pb))
    mutate_request = client.get_type("MutateCampaignBudgetsRequest")
    mutate_request.customer_id = cid
    mutate_request.operations.append(op)
    mutate_request.validate_only = validate_only
    service.mutate_campaign_budgets(request=mutate_request)
    return {"validated": validate_only, "daily_budget_amount": amount, "currency": currency}


def set_campaign_status(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = assert_allowed(request["customer_id"])
    currency, _ = write_currency_guard(client, cid)
    status = str(request["status"]).upper()
    if status not in {"PAUSED", "ENABLED"}:
        raise RuntimeError("status must be PAUSED or ENABLED")
    validate_only = bool(request.get("validate_only", True))
    service = client.get_service("CampaignService")
    op = client.get_type("CampaignOperation")
    op.update.resource_name = request["campaign_resource_name"]
    op.update.status = getattr(client.enums.CampaignStatusEnum, status)
    op.update_mask.CopyFrom(protobuf_helpers.field_mask(None, op.update._pb))
    mutate_request = client.get_type("MutateCampaignsRequest")
    mutate_request.customer_id = cid
    mutate_request.operations.append(op)
    mutate_request.validate_only = validate_only
    service.mutate_campaigns(request=mutate_request)
    return {"validated": validate_only, "status": status, "currency_guard": currency}


OPERATIONS = {
    "list_accessible_customers": list_accessible_customers,
    "account_preflight": account_preflight,
    "measurement_inspection": measurement_inspection,
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
