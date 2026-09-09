# Campaign media

Put campaign creative source files under this directory so GitHub Actions can read them.

Recommended Dot Connect layout:

```text
media/
  dot-connect/
    images/
      image-01.png
      image-02.png
    videos/
      video-01.mp4
      video-02.mp4
```

## Repository limits

Keep individual files below GitHub's normal 100 MiB file limit. This repository adds a stricter 95 MiB application guardrail. For larger media, use Git LFS or object storage rather than committing huge binaries directly.

## Upload flow

1. Commit images/videos under `media/`.
2. Add a JSON request under `media-requests/`.
3. A push of that request triggers the `Google Ads Media` workflow.
4. Use `dry_run: true` first to verify which repository files will be selected.
5. Change to `dry_run: false` only after reviewing the manifest.
6. Image uploads return Google Ads Asset resource names immediately.
7. Video uploads are asynchronous. Their upload resource names must later be passed to `finalize_video_assets` after Google reports them as processed. The finalization step creates the YouTube-video Asset resource names that an App ad can reference.

Example upload request:

```json
{
  "operation": "upload_media_assets",
  "customer_id": "8130408947",
  "asset_name_prefix": "Dot Connect | Phase 1",
  "image_paths": ["media/dot-connect/images/*.png"],
  "video_paths": ["media/dot-connect/videos/*.mp4"],
  "dry_run": true
}
```

Example video finalization request:

```json
{
  "operation": "finalize_video_assets",
  "customer_id": "8130408947",
  "asset_name_prefix": "Dot Connect | Phase 1",
  "video_upload_resource_names": [
    "customers/8130408947/youTubeVideoUploads/EXAMPLE"
  ]
}
```

The media workflow does not enable campaigns and does not change campaign budgets.
