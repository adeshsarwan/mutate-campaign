import json
import os
import sys
from pathlib import Path
from typing import Any

from google.ads.googleads.client import GoogleAdsClient


def clean_id(value: str | int) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def load_client() -> GoogleAdsClient:
    required = ["GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET", "GOOGLE_ADS_REFRESH_TOKEN"]
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


def assert_allowed(customer_id: str) -> str:
    cid = clean_id(customer_id)
    raw = os.environ.get("GOOGLE_ADS_ALLOWED_CUSTOMER_IDS", "")
    allowed = {clean_id(x) for x in raw.split(",") if clean_id(x)}
    if cid not in allowed:
        raise RuntimeError(f"Customer {cid} is not in GOOGLE_ADS_ALLOWED_CUSTOMER_IDS")
    return cid


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


def query_assets(client: GoogleAdsClient, cid: str, prefix: str) -> list[dict[str, Any]]:
    safe = prefix.replace("'", "\\'")
    rows = client.get_service("GoogleAdsService").search(
        customer_id=cid,
        query=f"""
            SELECT asset.resource_name, asset.id, asset.name, asset.type,
                   asset.image_asset.full_size.width_pixels,
                   asset.image_asset.full_size.height_pixels,
                   asset.youtube_video_asset.youtube_video_id
            FROM asset
            WHERE asset.name LIKE '{safe}%'
            ORDER BY asset.id
        """,
    )
    out = []
    for row in rows:
        a = row.asset
        out.append({
            "resource_name": a.resource_name,
            "id": str(a.id),
            "name": a.name,
            "type": a.type_.name,
            "width": int(a.image_asset.full_size.width_pixels or 0),
            "height": int(a.image_asset.full_size.height_pixels or 0),
            "youtube_video_id": a.youtube_video_asset.youtube_video_id,
        })
    return out


def inspect_app_campaign_assets(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = assert_allowed(request["customer_id"])
    campaign_id = clean_id(request["campaign_id"])
    ad_group_id = clean_id(request["ad_group_id"])
    ga = client.get_service("GoogleAdsService")
    rows = list(ga.search(customer_id=cid, query=f"""
        SELECT campaign.id, campaign.name, campaign.status,
               ad_group.id, ad_group.name, ad_group.status,
               ad_group_ad.resource_name, ad_group_ad.status, ad_group_ad.ad.id
        FROM ad_group_ad
        WHERE campaign.id = {campaign_id} AND ad_group.id = {ad_group_id}
    """))
    ads = []
    campaign_status = None
    for row in rows:
        campaign_status = row.campaign.status.name
        ads.append({
            "resource_name": row.ad_group_ad.resource_name,
            "status": row.ad_group_ad.status.name,
            "ad_id": str(row.ad_group_ad.ad.id),
        })
    prefix = str(request.get("asset_name_prefix") or "Dot Connect")
    assets = query_assets(client, cid, prefix)
    return {
        "customer_id": cid,
        "campaign_id": campaign_id,
        "ad_group_id": ad_group_id,
        "campaign_status": campaign_status,
        "ads": ads,
        "assets": assets,
        "image_assets": [a for a in assets if a["type"] == "IMAGE"],
        "video_assets": [a for a in assets if a["type"] == "YOUTUBE_VIDEO"],
    }


def attach_app_campaign_assets(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = assert_allowed(request["customer_id"])
    campaign_id = clean_id(request["campaign_id"])
    ad_group_id = clean_id(request["ad_group_id"])
    ad_group_resource = client.get_service("AdGroupService").ad_group_path(cid, ad_group_id)
    ga = client.get_service("GoogleAdsService")

    campaign_rows = list(ga.search(customer_id=cid, query=f"SELECT campaign.status FROM campaign WHERE campaign.id = {campaign_id}"))
    if not campaign_rows or campaign_rows[0].campaign.status.name != "PAUSED":
        raise RuntimeError("Safety block: campaign must exist and be PAUSED before replacing creatives.")

    headlines = [str(x) for x in request.get("headlines", [])]
    descriptions = [str(x) for x in request.get("descriptions", [])]
    images = [str(x) for x in request.get("image_asset_resource_names", [])][:20]
    videos = [str(x) for x in request.get("youtube_video_asset_resource_names", [])][:20]
    if not headlines or not descriptions:
        raise RuntimeError("At least one headline and one description are required.")
    if not images and not videos:
        raise RuntimeError("At least one image or video asset is required.")
    if len(headlines) > 5 or len(descriptions) > 5:
        raise RuntimeError("Maximum 5 headlines and 5 descriptions supported.")

    existing_rows = list(ga.search(customer_id=cid, query=f"""
        SELECT ad_group_ad.resource_name, ad_group_ad.status
        FROM ad_group_ad
        WHERE ad_group.id = {ad_group_id}
    """))
    existing = [r.ad_group_ad.resource_name for r in existing_rows if r.ad_group_ad.status.name != "REMOVED"]

    service = client.get_service("AdGroupAdService")
    operations = []
    create_op = client.get_type("AdGroupAdOperation")
    new_ad = create_op.create
    new_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    new_ad.ad_group = ad_group_resource
    new_ad.ad.app_ad.headlines.extend([text_asset(client, x) for x in headlines])
    new_ad.ad.app_ad.descriptions.extend([text_asset(client, x) for x in descriptions])
    new_ad.ad.app_ad.images.extend([image_asset(client, x) for x in images])
    new_ad.ad.app_ad.youtube_videos.extend([video_asset(client, x) for x in videos])
    operations.append(create_op)
    for resource_name in existing:
        remove_op = client.get_type("AdGroupAdOperation")
        remove_op.remove = resource_name
        operations.append(remove_op)

    mutate_request = client.get_type("MutateAdGroupAdsRequest")
    mutate_request.customer_id = cid
    mutate_request.operations.extend(operations)
    mutate_request.partial_failure = False
    mutate_request.validate_only = bool(request.get("validate_only", True))
    response = service.mutate_ad_group_ads(request=mutate_request)

    created_resource = ""
    if not request.get("validate_only", True) and response.results:
        created_resource = response.results[0].resource_name
    return {
        "validated": bool(request.get("validate_only", True)),
        "campaign_id": campaign_id,
        "ad_group_id": ad_group_id,
        "campaign_status": "PAUSED",
        "image_count": len(images),
        "video_count": len(videos),
        "removed_old_ads": existing if not request.get("validate_only", True) else [],
        "new_ad_group_ad_resource_name": created_resource,
    }


OPERATIONS = {
    "inspect_app_campaign_assets": inspect_app_campaign_assets,
    "attach_app_campaign_assets": attach_app_campaign_assets,
}


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python src/creative_attach.py REQUEST_JSON RESULT_JSON")
    request_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    request = json.loads(request_path.read_text())
    operation = request.get("operation")
    if operation not in OPERATIONS:
        raise RuntimeError(f"Unsupported creative operation: {operation}")
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
