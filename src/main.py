import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google.ads.googleads.client import GoogleAdsClient
from google.api_core import protobuf_helpers


LOCATION_IDS = {
    "United States": "2840",
    "United Kingdom": "2826",
    "Canada": "2124",
    "Australia": "2036",
    "New Zealand": "2554",
}

LANGUAGE_IDS = {
    "English": "1000",
}


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


def report_date_window(customer_info: dict[str, Any], request: dict[str, Any], default_days: int = 7) -> tuple[str, str, str]:
    """Return an inclusive GAQL date clause using the Google Ads account timezone.

    Google Ads predefined ranges such as LAST_7_DAYS exclude the current day. That
    caused same-day App campaign spend to appear as zero in our reports. Explicit
    BETWEEN dates keep the API result aligned with the Google Ads UI.
    """
    tz_name = str(customer_info.get("time_zone") or "UTC")
    try:
        today = datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        today = datetime.now(ZoneInfo("UTC")).date()
        tz_name = "UTC"

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
            "resource_name": ca.resource_name,
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


def resolve_conversion_action(client: GoogleAdsClient, cid: str, event_name: str) -> str:
    matches = [a for a in conversion_action_rows(client, cid) if event_match(a, event_name)]
    enabled = [a for a in matches if a["status"] == "ENABLED"]
    pool = enabled or matches
    if not pool:
        raise RuntimeError(f"No Google Ads conversion action found for event '{event_name}'.")
    if len(pool) > 1:
        primary = [a for a in pool if a["primary_for_goal"]]
        if len(primary) == 1:
            pool = primary
        else:
            raise RuntimeError(f"Multiple conversion actions match '{event_name}'; specify conversion_action_resource_name.")
    return pool[0]["resource_name"]


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
    info = get_customer_info(client, cid)
    currency = info["currency"]
    date_clause, start_date, end_date = report_date_window(info, request, default_days=30)
    query = f"""
        SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value,
               metrics.all_conversions, metrics.all_conversions_value
        FROM campaign
        WHERE {date_clause}
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
            "impressions": int(row.metrics.impressions),
            "clicks": int(row.metrics.clicks),
            "cost_amount": row.metrics.cost_micros / 1_000_000,
            "currency": currency,
            "conversions": float(row.metrics.conversions),
            "conversion_value": float(row.metrics.conversions_value),
            "all_conversions": float(row.metrics.all_conversions),
            "all_conversion_value": float(row.metrics.all_conversions_value),
        })
    return {
        "customer_id": cid,
        "start_date": start_date,
        "end_date": end_date,
        "time_zone": info["time_zone"],
        "currency": currency,
        "campaigns": out,
    }


def delivery_diagnostic(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = clean_id(request["customer_id"])
    campaign_id = clean_id(request["campaign_id"])
    info = get_customer_info(client, cid)
    currency = info["currency"]
    date_clause, start_date, end_date = report_date_window(info, request, default_days=7)
    service = client.get_service("GoogleAdsService")

    campaign_rows = list(service.search(customer_id=cid, query=f"""
        SELECT campaign.id, campaign.name, campaign.status, campaign.primary_status,
               campaign.primary_status_reasons, campaign.advertising_channel_type,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.all_conversions
        FROM campaign
        WHERE campaign.id = {campaign_id}
          AND {date_clause}
    """))

    ad_group_rows = list(service.search(customer_id=cid, query=f"""
        SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.primary_status,
               ad_group.primary_status_reasons,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.all_conversions
        FROM ad_group
        WHERE campaign.id = {campaign_id}
          AND {date_clause}
        ORDER BY ad_group.id
    """))

    ad_rows = list(service.search(customer_id=cid, query=f"""
        SELECT ad_group.id, ad_group.name,
               ad_group_ad.ad.id, ad_group_ad.status,
               ad_group_ad.policy_summary.approval_status,
               ad_group_ad.policy_summary.review_status,
               metrics.impressions, metrics.clicks, metrics.cost_micros
        FROM ad_group_ad
        WHERE campaign.id = {campaign_id}
          AND {date_clause}
        ORDER BY ad_group.id, ad_group_ad.ad.id
    """))

    return {
        "customer_id": cid,
        "campaign_id": campaign_id,
        "start_date": start_date,
        "end_date": end_date,
        "time_zone": info["time_zone"],
        "currency": currency,
        "campaign": [{
            "id": str(r.campaign.id),
            "name": r.campaign.name,
            "status": r.campaign.status.name,
            "primary_status": r.campaign.primary_status.name,
            "primary_status_reasons": [x.name for x in r.campaign.primary_status_reasons],
            "impressions": int(r.metrics.impressions),
            "clicks": int(r.metrics.clicks),
            "cost_amount": r.metrics.cost_micros / 1_000_000,
            "conversions": float(r.metrics.conversions),
            "all_conversions": float(r.metrics.all_conversions),
        } for r in campaign_rows],
        "ad_groups": [{
            "id": str(r.ad_group.id),
            "name": r.ad_group.name,
            "status": r.ad_group.status.name,
            "primary_status": r.ad_group.primary_status.name,
            "primary_status_reasons": [x.name for x in r.ad_group.primary_status_reasons],
            "impressions": int(r.metrics.impressions),
            "clicks": int(r.metrics.clicks),
            "cost_amount": r.metrics.cost_micros / 1_000_000,
            "conversions": float(r.metrics.conversions),
            "all_conversions": float(r.metrics.all_conversions),
        } for r in ad_group_rows],
        "ads": [{
            "ad_group_id": str(r.ad_group.id),
            "ad_group_name": r.ad_group.name,
            "ad_id": str(r.ad_group_ad.ad.id),
            "status": r.ad_group_ad.status.name,
            "approval_status": r.ad_group_ad.policy_summary.approval_status.name,
            "review_status": r.ad_group_ad.policy_summary.review_status.name,
            "impressions": int(r.metrics.impressions),
            "clicks": int(r.metrics.clicks),
            "cost_amount": r.metrics.cost_micros / 1_000_000,
        } for r in ad_rows],
    }


def normalized_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(x) for x in value]


def validate_text_assets(headlines: list[str], descriptions: list[str]) -> None:
    if not headlines or not descriptions:
        raise RuntimeError("A complete App ad requires at least one headline and one description.")
    if len(headlines) > 5 or len(descriptions) > 5:
        raise RuntimeError("App ad text is limited to at most 5 headlines and 5 descriptions in this wrapper.")
    too_long_headlines = [x for x in headlines if len(x) > 30]
    too_long_descriptions = [x for x in descriptions if len(x) > 90]
    if too_long_headlines:
        raise RuntimeError(f"Headline exceeds 30 characters: {too_long_headlines[0]}")
    if too_long_descriptions:
        raise RuntimeError(f"Description exceeds 90 characters: {too_long_descriptions[0]}")


def text_asset(client: GoogleAdsClient, value: str):
    asset = client.get_type("AdTextAsset")
    asset.text = value
    return asset


def image_asset(client: GoogleAdsClient, resource_name: str):
    asset = client.get_type("AdImageAsset")
    asset.asset = resource_name
    return asset


def video_asset(client: GoogleAdsClient, resource_name: str):
    asset = client.get_type("AdVideoAsset")
    asset.asset = resource_name
    return asset


def create_app_campaign_draft(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = assert_allowed(request["customer_id"])
    currency, limit = write_currency_guard(client, cid)
    daily_budget_amount = request_budget_amount(request, currency)
    check_budget_limit(daily_budget_amount, limit, currency)
    validate_only = bool(request.get("validate_only", True))

    target_locations = normalized_list(request.get("target_locations"))
    target_languages = normalized_list(request.get("target_languages") or request.get("target_language"))
    headlines = normalized_list(request.get("headlines"))
    descriptions = normalized_list(request.get("descriptions"))
    image_resources = normalized_list(request.get("image_asset_resource_names"))
    video_resources = normalized_list(request.get("youtube_video_asset_resource_names"))
    validate_text_assets(headlines, descriptions)

    unknown_locations = [x for x in target_locations if x not in LOCATION_IDS]
    if unknown_locations:
        raise RuntimeError(f"Unsupported target location(s): {', '.join(unknown_locations)}")
    unknown_languages = [x for x in target_languages if x not in LANGUAGE_IDS]
    if unknown_languages:
        raise RuntimeError(f"Unsupported target language(s): {', '.join(unknown_languages)}")
    if not target_locations:
        raise RuntimeError("At least one target location is required to avoid accidental worldwide targeting.")
    if not target_languages:
        raise RuntimeError("At least one target language is required.")

    conversion_action_resource = request.get("conversion_action_resource_name")
    optimization_goal_event = request.get("optimization_goal_event") or request.get("optimization_goal")
    if not conversion_action_resource and optimization_goal_event:
        conversion_action_resource = resolve_conversion_action(client, cid, str(optimization_goal_event))

    budget_service = client.get_service("CampaignBudgetService")
    campaign_service = client.get_service("CampaignService")
    ad_group_service = client.get_service("AdGroupService")
    geo_service = client.get_service("GeoTargetConstantService")
    google_ads_service = client.get_service("GoogleAdsService")

    budget_resource = budget_service.campaign_budget_path(cid, -1)
    campaign_resource = campaign_service.campaign_path(cid, -2)
    ad_group_resource = ad_group_service.ad_group_path(cid, -3)

    operations = []

    budget_mutate = client.get_type("MutateOperation")
    budget = budget_mutate.campaign_budget_operation.create
    budget.resource_name = budget_resource
    budget.name = f"{request['name']} budget"
    budget.amount_micros = amount_to_micros(daily_budget_amount)
    budget.explicitly_shared = False
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    operations.append(budget_mutate)

    campaign_mutate = client.get_type("MutateOperation")
    campaign = campaign_mutate.campaign_operation.create
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

    if conversion_action_resource:
        campaign.selective_optimization.conversion_actions.append(conversion_action_resource)
    operations.append(campaign_mutate)

    for location_name in target_locations:
        criterion_mutate = client.get_type("MutateOperation")
        criterion = criterion_mutate.campaign_criterion_operation.create
        criterion.campaign = campaign_resource
        criterion.location.geo_target_constant = geo_service.geo_target_constant_path(LOCATION_IDS[location_name])
        operations.append(criterion_mutate)

    for language_name in target_languages:
        criterion_mutate = client.get_type("MutateOperation")
        criterion = criterion_mutate.campaign_criterion_operation.create
        criterion.campaign = campaign_resource
        criterion.language.language_constant = google_ads_service.language_constant_path(LANGUAGE_IDS[language_name])
        operations.append(criterion_mutate)

    ad_group_mutate = client.get_type("MutateOperation")
    ad_group = ad_group_mutate.ad_group_operation.create
    ad_group.resource_name = ad_group_resource
    ad_group.name = request.get("ad_group_name") or f"{request['name']} | Ad Group 1"
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    ad_group.campaign = campaign_resource
    operations.append(ad_group_mutate)

    ad_mutate = client.get_type("MutateOperation")
    ad_group_ad = ad_mutate.ad_group_ad_operation.create
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad_group_ad.ad_group = ad_group_resource
    ad_group_ad.ad.app_ad.headlines.extend([text_asset(client, x) for x in headlines])
    ad_group_ad.ad.app_ad.descriptions.extend([text_asset(client, x) for x in descriptions])
    if image_resources:
        ad_group_ad.ad.app_ad.images.extend([image_asset(client, x) for x in image_resources])
    if video_resources:
        ad_group_ad.ad.app_ad.youtube_videos.extend([video_asset(client, x) for x in video_resources])
    operations.append(ad_mutate)

    mutate_request = client.get_type("MutateGoogleAdsRequest")
    mutate_request.customer_id = cid
    mutate_request.mutate_operations.extend(operations)
    mutate_request.partial_failure = False
    mutate_request.validate_only = validate_only
    response = google_ads_service.mutate(request=mutate_request)

    manifest = {
        "campaign": request["name"],
        "budget": {"amount": daily_budget_amount, "currency": currency},
        "status": "PAUSED",
        "locations": target_locations,
        "languages": target_languages,
        "optimization_goal_event": optimization_goal_event,
        "conversion_action_resource_name": conversion_action_resource,
        "ad_group": request.get("ad_group_name") or f"{request['name']} | Ad Group 1",
        "creative": {
            "headlines": headlines,
            "descriptions": descriptions,
            "image_asset_resource_names": image_resources,
            "youtube_video_asset_resource_names": video_resources,
        },
        "operation_count": len(operations),
    }

    if validate_only:
        return {
            "validated": True,
            "created": False,
            "operation": "create_app_campaign_draft",
            "customer_id": cid,
            "manifest": manifest,
            "note": "Complete App campaign structure validated atomically. No Google Ads resources were created.",
        }

    responses = response.mutate_operation_responses
    return {
        "validated": False,
        "created": True,
        "customer_id": cid,
        "manifest": manifest,
        "budget_resource_name": responses[0].campaign_budget_result.resource_name,
        "campaign_resource_name": responses[1].campaign_result.resource_name,
        "ad_group_resource_name": next(
            x.ad_group_result.resource_name for x in responses if x.ad_group_result.resource_name
        ),
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
    op.update.explicitly_shared = False
    op.update.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
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
    "delivery_diagnostic": delivery_diagnostic,
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
