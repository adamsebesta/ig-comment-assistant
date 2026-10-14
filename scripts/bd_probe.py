#!/usr/bin/env python3
"""
Step-zero probe: does Instagram Business Discovery return caption + media_url
for other Business/Creator accounts?

SAFETY — this script is deliberately boring:
  - GET requests only, through the official Graph API. It never posts,
    comments, likes, or follows anything.
  - Hard cap of 5 handles, one request per handle, 5 s apart. That's ~0.03%
    of the documented 200 calls/hour budget.
  - Stops on the first API error instead of retrying.

Usage:
  export IG_USER_ID=...        # your brand account's IG user id (not the @handle)
  export FB_TOKEN=...          # Facebook User token: instagram_basic,
                               #   instagram_manage_insights, pages_read_engagement
  export GRAPH_VERSION=v23.0   # optional; check the current version in your app dashboard
  python3 bd_probe.py handle1 handle2 handle3   # include one Reels-heavy account

Only stdlib, no pip install needed.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_HANDLES = 5
DELAY_S = 5
MEDIA_FIELDS = (
    "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,"
    "like_count,comments_count,children{media_type,media_url}"
)
CHECK = ["caption", "media_url", "thumbnail_url", "permalink", "timestamp",
         "like_count", "comments_count"]


def probe(base, ig_user_id, token, handle):
    fields = (
        f"business_discovery.username({handle})"
        f"{{username,followers_count,media_count,media.limit(5){{{MEDIA_FIELDS}}}}}"
    )
    url = f"{base}/{ig_user_id}?" + urllib.parse.urlencode(
        {"fields": fields, "access_token": token})
    with urllib.request.urlopen(url, timeout=30) as resp:
        usage = resp.headers.get("X-App-Usage")
        return json.load(resp), usage


def main():
    handles = [h.lstrip("@") for h in sys.argv[1:]]
    if not handles:
        sys.exit(__doc__)
    if len(handles) > MAX_HANDLES:
        sys.exit(f"Refusing more than {MAX_HANDLES} handles — this is a probe, not a poller.")

    ig_user_id = os.environ["IG_USER_ID"]
    token = os.environ["FB_TOKEN"]
    version = os.environ.get("GRAPH_VERSION", "").strip("/")
    base = "https://graph.facebook.com" + (f"/{version}" if version else "")

    for i, handle in enumerate(handles):
        if i:
            time.sleep(DELAY_S)
        print(f"\n=== @{handle} ===")
        try:
            data, usage = probe(base, ig_user_id, token, handle)
        except urllib.error.HTTPError as e:
            # Don't retry: print Meta's error and stop. Code 110/100 usually
            # means the handle isn't a Business/Creator account.
            print(f"HTTP {e.code}: {e.read().decode(errors='replace')}")
            print("Stopping on first error (by design).")
            return

        bd = data.get("business_discovery", {})
        media = bd.get("media", {}).get("data", [])
        print(f"followers={bd.get('followers_count')} media_count={bd.get('media_count')} "
              f"posts_returned={len(media)}  X-App-Usage={usage}")
        for m in media:
            present = " ".join(f"{k}={'Y' if m.get(k) is not None else '-'}" for k in CHECK)
            kids = m.get("children", {}).get("data", [])
            kid_urls = sum(1 for c in kids if c.get("media_url"))
            extra = f" children_with_url={kid_urls}/{len(kids)}" if kids else ""
            print(f"  {m.get('media_type'):<15} {present}{extra}")
            print(f"    {m.get('permalink')}")


if __name__ == "__main__":
    main()
