"""Runtime compatibility patches for the Google Ads Python client.

The google-ads 31.4.0 v25 resumable YouTube upload helper calls
raise_formatted_for_status(response) even though the helper now requires
(response, version). Patch only that imported function until upstream fixes it.
"""

try:
    from google.ads.googleads import errors as googleads_errors
    from google.ads.googleads.v25.services.services.you_tube_video_upload_service.transports import resumable_upload

    def _raise_formatted_for_status_v25(response):
        return googleads_errors.raise_formatted_for_status(response, "v25")

    resumable_upload.raise_formatted_for_status = _raise_formatted_for_status_v25
except Exception:
    # Do not break unrelated commands if the Google Ads package/version changes.
    pass
