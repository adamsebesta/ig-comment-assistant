"""Poll due sources, store new posts + their images. Runs as a CronJob every 15 min.

Each run takes at most MAX_CALLS_PER_RUN sources, hot tier first, spaced
PACE_SECONDS apart, and stops early on throttling or high X-App-Usage.
Bursting all sources at once is what gets an app throttled.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from . import config, db
from .meta import MetaClient, MetaError

log = logging.getLogger("ingest")

DUE_SQL = """
SELECT * FROM sources
WHERE active AND (
    last_polled IS NULL
    OR last_polled < now() - make_interval(mins => CASE
        WHEN kind = 'hashtag' THEN %(hashtag)s
        WHEN tier = 'hot' THEN %(hot)s
        ELSE %(normal)s END)
)
ORDER BY (tier = 'hot') DESC, last_polled ASC NULLS FIRST
LIMIT %(limit)s
"""


def pick_image(m):
    """Best still image for the vision model: photo > video cover > first carousel photo."""
    if m.get("media_type") != "VIDEO" and m.get("media_url"):
        return m["media_url"]
    if m.get("thumbnail_url"):
        return m["thumbnail_url"]
    for c in m.get("children", {}).get("data", []):
        if c.get("media_type") == "IMAGE" and c.get("media_url"):
            return c["media_url"]
    return None


def store(conn, client, src, media, owner):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.MAX_POST_AGE_HOURS)
    new = 0
    for m in media:
        posted_at = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        if posted_at < cutoff:
            continue
        if conn.execute("SELECT 1 FROM posts WHERE media_id = %s", (m["id"],)).fetchone():
            continue
        image, image_type = None, None
        url = pick_image(m)
        if url:
            try:
                image, image_type = client.download(url)
            except Exception as e:  # a missing image shouldn't drop the post
                log.warning("image download failed for %s: %s", m["id"], e)
        conn.execute(
            """INSERT INTO posts (media_id, source_id, owner, caption, media_type, permalink,
                   posted_at, like_count, comments_count, image, image_type)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (media_id) DO NOTHING""",
            (m["id"], src["id"], owner, m.get("caption"), m.get("media_type"), m.get("permalink"),
             posted_at, m.get("like_count"), m.get("comments_count"), image, image_type),
        )
        new += 1
    return new


def fetch(conn, client, src):
    """Returns (media_count, media, owner) for one source."""
    if src["kind"] == "handle":
        media_count, media = client.business_discovery(src["value"], config.MEDIA_PER_SOURCE)
        return media_count, media, src["value"]

    tag = src["value"]
    if not db.hashtag_in_window(conn, tag) and db.hashtag_budget_used(conn) >= config.HASHTAG_BUDGET:
        raise MetaError(None, None, f"hashtag budget ({config.HASHTAG_BUDGET}/7d) full, skipping #{tag}")
    conn.execute("INSERT INTO hashtag_queries (tag) VALUES (%s)", (tag,))
    hashtag_id = src["hashtag_id"]
    if not hashtag_id:
        hashtag_id = client.hashtag_id(tag)
        if not hashtag_id:
            raise MetaError(None, None, f"#{tag} not found")
        conn.execute("UPDATE sources SET hashtag_id = %s WHERE id = %s", (hashtag_id, src["id"]))
    return None, client.hashtag_recent(hashtag_id, config.MEDIA_PER_SOURCE), None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # httpx logs full request URLs at INFO, and Graph API URLs carry access_token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    conn = db.connect()
    db.init(conn)
    client = MetaClient()

    due = conn.execute(DUE_SQL, {
        "hashtag": config.HASHTAG_INTERVAL_MIN, "hot": config.HOT_INTERVAL_MIN,
        "normal": config.NORMAL_INTERVAL_MIN, "limit": config.MAX_CALLS_PER_RUN,
    }).fetchall()
    log.info("%d sources due", len(due))

    for i, src in enumerate(due):
        if client.usage >= config.USAGE_CEILING:
            log.warning("X-App-Usage at %s%%, stopping run early", client.usage)
            break
        if i:
            time.sleep(config.PACE_SECONDS)
        label = ("@" if src["kind"] == "handle" else "#") + src["value"]
        try:
            media_count, media, owner = fetch(conn, client, src)
        except MetaError as e:
            log.error("%s: %s", label, e)
            conn.execute("UPDATE sources SET last_polled = now(), last_error = %s WHERE id = %s",
                         (str(e), src["id"]))
            if e.throttled:
                log.warning("throttled, stopping run")
                break
            continue
        n = store(conn, client, src, media, owner)
        conn.execute(
            """UPDATE sources SET last_polled = now(), last_error = NULL,
                   last_media_count = COALESCE(%s, last_media_count) WHERE id = %s""",
            (media_count, src["id"]),
        )
        log.info("%s: %d new posts (usage %s%%)", label, n, client.usage)


if __name__ == "__main__":
    main()
