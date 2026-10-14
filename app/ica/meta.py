"""Read-only Instagram Graph API client.

HARD RULE: this module only ever issues GET requests. No method here may
post, comment, like, follow or message. Posting is a human action in the
Instagram app; that's what keeps the brand account safe.
"""
import json
import logging

import httpx

from . import config

log = logging.getLogger(__name__)

# Business Discovery: verified by scripts/bd_probe.py on 2026-10-14. Videos
# never return media_url, only thumbnail_url (the cover frame).
MEDIA_FIELDS = (
    "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,"
    "like_count,comments_count,children{media_type,media_url}"
)
# Hashtag recent_media doesn't document thumbnail_url or username.
HASHTAG_MEDIA_FIELDS = (
    "id,caption,media_type,media_url,permalink,timestamp,"
    "like_count,comments_count,children{media_type,media_url}"
)
# 4 app-level, 17 user-level, 32 page-level, 613 custom throttle.
THROTTLE_CODES = {4, 17, 32, 613}


class MetaError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f"HTTP {status} code={code}: {message}")
        self.code = code

    @property
    def throttled(self):
        return self.code in THROTTLE_CODES


class MetaClient:
    def __init__(self):
        v = config.GRAPH_VERSION.strip("/")
        self.base = "https://graph.facebook.com" + (f"/{v}" if v else "")
        self.http = httpx.Client(timeout=30)
        self.usage = 0  # highest % reported by X-App-Usage on the last call

    def _get(self, path, **params):
        params["access_token"] = config.META_TOKEN
        r = self.http.get(f"{self.base}/{path}", params=params)
        self._track_usage(r.headers.get("x-app-usage"))
        try:
            body = r.json()
        except ValueError:
            raise MetaError(r.status_code, None, r.text[:200])
        if r.status_code != 200 or "error" in body:
            err = body.get("error", {})
            raise MetaError(r.status_code, err.get("code"), err.get("message", ""))
        return body

    def _track_usage(self, header):
        if not header:
            return
        u = json.loads(header)
        self.usage = max(u.get("call_count", 0), u.get("total_time", 0), u.get("total_cputime", 0))

    def business_discovery(self, handle, limit):
        fields = (
            f"business_discovery.username({handle})"
            f"{{username,media_count,media.limit({limit}){{{MEDIA_FIELDS}}}}}"
        )
        bd = self._get(config.IG_USER_ID, fields=fields)["business_discovery"]
        return bd.get("media_count"), bd.get("media", {}).get("data", [])

    def hashtag_id(self, tag):
        # This lookup is what counts against the 30-unique-hashtags/7-days limit.
        data = self._get("ig_hashtag_search", user_id=config.IG_USER_ID, q=tag)["data"]
        return data[0]["id"] if data else None

    def hashtag_recent(self, hashtag_id, limit):
        try:
            return self._get(
                f"{hashtag_id}/recent_media",
                user_id=config.IG_USER_ID, fields=HASHTAG_MEDIA_FIELDS, limit=limit,
            ).get("data", [])
        except MetaError as e:
            # Busy tags full of big carousels (#sprezzatura) fail with code 1
            # "reduce the amount of data". Retry once smaller: fewer posts, and
            # carousel slides only as far as the first image.
            if e.code != 1:
                raise
            return self._get(
                f"{hashtag_id}/recent_media", user_id=config.IG_USER_ID, limit=max(1, limit // 2),
                fields=HASHTAG_MEDIA_FIELDS.replace("children{media_type,media_url}",
                                                    "children.limit(1){media_type,media_url}"),
            ).get("data", [])

    def download(self, url):
        # Signed CDN URL that expires within days, so grab the bytes now.
        # No token on this request; it isn't a Graph API call.
        r = self.http.get(url, follow_redirects=True)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/jpeg")
