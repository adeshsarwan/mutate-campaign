import glob
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from google.ads.googleads.client import GoogleAdsClient


REPO_ROOT = Path(__file__).resolve().parents[1]
MEDIA_ROOT = (REPO_ROOT / "media").resolve()
ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}
ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov"}
MAX_REPO_MEDIA_BYTES = 95 * 1024 * 1024


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
        raise RuntimeError("GOOGLE_ADS_ALLOWED_CUSTOMER_IDS is empty; media writes are disabled.")
    if cid not in allowed:
        raise RuntimeError(f"Customer {cid} is not in GOOGLE_ADS_ALLOWED_CUSTOMER_IDS")
    return cid


def ensure_media_path(path: Path) -> Path:
    resolved = path.resolve()
    if resolved != MEDIA_ROOT and MEDIA_ROOT not in resolved.parents:
        raise RuntimeError(f"Media path must stay under media/: {path}")
    if not resolved.is_file():
        raise RuntimeError(f"Media file does not exist: {path}")
    if resolved.stat().st_size > MAX_REPO_MEDIA_BYTES:
        raise RuntimeError(f"Media file exceeds 95 MiB repository guardrail: {path}")
    return resolved


def expand_paths(patterns: Any, allowed_suffixes: set[str]) -> list[Path]:
    if not patterns:
        return []
    if isinstance(patterns, str):
        patterns = [patterns]
    found: list[Path] = []
    seen: set[str] = set()
    for raw_pattern in patterns:
        pattern = str(raw_pattern).replace("\\", "/")
        if pattern.startswith("/") or ".." in Path(pattern).parts:
            raise RuntimeError(f"Unsafe media pattern: {pattern}")
        if not pattern.startswith("media/"):
            raise RuntimeError(f"Media patterns must start with media/: {pattern}")
        matches = glob.glob(str(REPO_ROOT / pattern), recursive=True)
        if not matches:
            raise RuntimeError(f"No files matched media pattern: {pattern}")
        for match in matches:
            path = ensure_media_path(Path(match))
            if path.suffix.lower() not in allowed_suffixes:
                raise RuntimeError(f"Unsupported media type: {path.name}")
            key = str(path)
            if key not in seen:
                seen.add(key)
                found.append(path)
    return sorted(found)


def relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)).replace("\\", "/")


def sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def image_mime_enum(client: GoogleAdsClient, suffix: str):
    suffix = suffix.lower()
    if suffix == ".png":
        return client.enums.MimeTypeEnum.IMAGE_PNG
    if suffix in {".jpg", ".jpeg"}:
        return client.enums.MimeTypeEnum.IMAGE_JPEG
    if suffix == ".gif":
        return client.enums.MimeTypeEnum.IMAGE_GIF
    raise RuntimeError(f"Unsupported image suffix: {suffix}")


def inspect_repo_media(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    images = expand_paths(request.get("image_paths"), ALLOWED_IMAGE_SUFFIXES)
    videos = expand_paths(request.get("video_paths"), ALLOWED_VIDEO_SUFFIXES)
    return {
        "images": [{"path": relative(p), "bytes": p.stat().st_size} for p in images],
        "videos": [{"path": relative(p), "bytes": p.stat().st_size} for p in videos],
        "image_count": len(images),
        "video_count": len(videos),
    }


def upload_image_assets(client: GoogleAdsClient, cid: str, paths: list[Path], prefix: str) -> list[dict[str, Any]]:
    if not paths:
        return []
    service = client.get_service("AssetService")
    operations = []
    metadata = []
    for index, path in enumerate(paths, start=1):
        data = path.read_bytes()
        op = client.get_type("AssetOperation")
        asset = op.create
        asset.type_ = client.enums.AssetTypeEnum.IMAGE
        asset.name = f"{prefix} | IMG {index:02d} | {sha12(data)}"
        asset.image_asset.data = data
        asset.image_asset.file_size = len(data)
        asset.image_asset.mime_type = image_mime_enum(client, path.suffix)
        operations.append(op)
        metadata.append({"path": relative(path), "name": asset.name, "bytes": len(data)})
    response = service.mutate_assets(customer_id=cid, operations=operations)
    if len(response.results) != len(metadata):
        raise RuntimeError("Google Ads returned an unexpected number of image asset results.")
    for item, result in zip(metadata, response.results):
        item["asset_resource_name"] = result.resource_name
    return metadata


def upload_videos(client: GoogleAdsClient, cid: str, paths: list[Path], prefix: str) -> list[dict[str, Any]]:
    if not paths:
        return []
    service = client.get_service("YouTubeVideoUploadService")
    results = []
    for index, path in enumerate(paths, start=1):
        request = client.get_type("CreateYouTubeVideoUploadRequest")
        request.customer_id = cid
        request.you_tube_video_upload.video_title = f"{prefix} | Video {index:02d}"
        request.you_tube_video_upload.video_description = "Gameplay creative uploaded through the Google Ads API."
        request.you_tube_video_upload.video_privacy = client.enums.YouTubeVideoPrivacyEnum.UNLISTED
        with path.open("rb") as stream:
            response = service.create_you_tube_video_upload(stream=stream, request=request, retry=None)
        results.append({
            "path": relative(path),
            "bytes": path.stat().st_size,
            "video_upload_resource_name": response.resource_name,
            "state": "UPLOADING_OR_PROCESSING",
        })
    return results


def upload_media_assets(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = assert_allowed(request["customer_id"])
    images = expand_paths(request.get("image_paths"), ALLOWED_IMAGE_SUFFIXES)
    videos = expand_paths(request.get("video_paths"), ALLOWED_VIDEO_SUFFIXES)
    if not images and not videos:
        raise RuntimeError("No image or video files were selected.")
    prefix = str(request.get("asset_name_prefix") or "App Campaign Media").strip()
    dry_run = bool(request.get("dry_run", True))
    manifest = inspect_repo_media(client, request)
    if dry_run:
        return {"dry_run": True, "customer_id": cid, "manifest": manifest}
    return {
        "dry_run": False,
        "customer_id": cid,
        "images": upload_image_assets(client, cid, images, prefix),
        "video_uploads": upload_videos(client, cid, videos, prefix),
        "note": "Images are immediately usable Asset resources. Videos must reach PROCESSED before finalizing them as YouTube video Assets.",
    }


def video_upload_status(client: GoogleAdsClient, cid: str, resource_names: list[str]) -> list[dict[str, Any]]:
    if not resource_names:
        return []
    quoted = ", ".join("'" + x.replace("'", "\\'") + "'" for x in resource_names)
    query = f"""
        SELECT you_tube_video_upload.resource_name,
               you_tube_video_upload.video_id,
               you_tube_video_upload.state
        FROM you_tube_video_upload
        WHERE you_tube_video_upload.resource_name IN ({quoted})
    """
    rows = client.get_service("GoogleAdsService").search(customer_id=cid, query=query)
    out = []
    for row in rows:
        upload = row.you_tube_video_upload
        out.append({
            "video_upload_resource_name": upload.resource_name,
            "video_id": upload.video_id,
            "state": upload.state.name,
        })
    return out


def finalize_video_assets(client: GoogleAdsClient, request: dict[str, Any]) -> dict[str, Any]:
    cid = assert_allowed(request["customer_id"])
    names = request.get("video_upload_resource_names") or []
    if isinstance(names, str):
        names = [names]
    names = [str(x) for x in names]
    if not names:
        raise RuntimeError("video_upload_resource_names is required.")
    statuses = video_upload_status(client, cid, names)
    by_name = {x["video_upload_resource_name"]: x for x in statuses}
    missing = [x for x in names if x not in by_name]
    if missing:
        raise RuntimeError(f"Video upload resource not found: {missing[0]}")

    service = client.get_service("AssetService")
    operations = []
    ready = []
    pending = []
    for item in statuses:
        if item["state"] != "PROCESSED" or not item["video_id"]:
            pending.append(item)
            continue
        op = client.get_type("AssetOperation")
        asset = op.create
        asset.type_ = client.enums.AssetTypeEnum.YOUTUBE_VIDEO
        asset.name = f"{request.get('asset_name_prefix', 'App Campaign Media')} | YT {item['video_id']}"
        asset.youtube_video_asset.youtube_video_id = item["video_id"]
        operations.append(op)
        ready.append(item)

    if not operations:
        return {"customer_id": cid, "created_video_assets": [], "pending": pending}

    response = service.mutate_assets(customer_id=cid, operations=operations)
    created = []
    for item, result in zip(ready, response.results):
        created.append({**item, "asset_resource_name": result.resource_name})
    return {"customer_id": cid, "created_video_assets": created, "pending": pending}


OPERATIONS = {
    "inspect_repo_media": inspect_repo_media,
    "upload_media_assets": upload_media_assets,
    "finalize_video_assets": finalize_video_assets,
}


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python src/media_upload.py REQUEST_JSON RESULT_JSON")
    request_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    request = json.loads(request_path.read_text())
    operation = request.get("operation")
    if operation not in OPERATIONS:
        raise RuntimeError(f"Unsupported media operation: {operation}")
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
