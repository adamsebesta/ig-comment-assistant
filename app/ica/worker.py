"""Score new posts, draft comments for relevant ones, enforce retention.
Runs as a CronJob every 30 min."""
import logging
from pathlib import Path

from psycopg.types.json import Jsonb

from . import config, db, llm

log = logging.getLogger("worker")


def purge(conn):
    # Other people's posts go after RETENTION_DAYS. decisions.post_id becomes
    # NULL but the decision row (our own data, the learning signal) stays.
    n = conn.execute(
        "DELETE FROM posts WHERE created_at < now() - make_interval(days => %s)",
        (config.RETENTION_DAYS,),
    ).rowcount
    conn.execute("DELETE FROM hashtag_queries WHERE queried_at < now() - interval '8 days'")
    # A draft for a stale post isn't worth reviewing.
    conn.execute(
        "UPDATE posts SET status = 'expired' WHERE status = 'ready' "
        "AND posted_at < now() - make_interval(hours => %s)",
        (config.MAX_POST_AGE_HOURS,),
    )
    if n:
        log.info("purged %d posts past retention", n)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # httpx logs full request URLs at INFO, and Graph API URLs carry access_token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    conn = db.connect()
    db.init(conn)
    purge(conn)

    tone_path = Path(config.TONE_FILE)
    tone = tone_path.read_text() if tone_path.exists() else ""
    if not tone:
        log.warning("no tone file at %s, drafting without brand voice", tone_path)

    posts = conn.execute(
        "SELECT p.*, COALESCE(s.category, 'general') AS category FROM posts p "
        "LEFT JOIN sources s ON s.id = p.source_id "
        "WHERE p.status = 'new' ORDER BY p.created_at LIMIT %s",
        (config.WORKER_BATCH,),
    ).fetchall()
    log.info("%d posts to score", len(posts))
    recent = [r["final_text"] for r in conn.execute(
        "SELECT final_text FROM decisions WHERE action IN ('approve', 'edit') "
        "ORDER BY decided_at DESC LIMIT 15"
    )]

    for p in posts:
        try:
            s = llm.score(p, tone, p["category"])
            relevant = s["relevance"] >= config.RELEVANCE_THRESHOLD
            conn.execute(
                "UPDATE posts SET relevance = %s, mood = %s, score_reason = %s, signals = %s, model = %s, "
                "status = %s WHERE id = %s",
                (s["relevance"], s["mood"], s["reason"], Jsonb(s["signals"]), config.OLLAMA_MODEL,
                 "scored" if relevant else "skipped", p["id"]),
            )
            if not relevant:
                sig = s["signals"]
                log.info("post %s skipped (%.2f) [%s, focus=%s, trousers=%s, topics=%s]: %s",
                         p["id"], s["relevance"], p["category"], sig["clothing_focus"],
                         sig["trousers_visible"], ",".join(sig["topics"]) or "-", s["reason"])
                continue
            comments, rejected = llm.draft(p, s, tone, recent, p["category"])
            for text, why in rejected:
                log.info("post %s draft rejected (%s): %s", p["id"], why, text)
            for c in comments:
                conn.execute(
                    "INSERT INTO drafts (post_id, text, prompt_version, model) VALUES (%s, %s, %s, %s)",
                    (p["id"], c, llm.PROMPT_VERSION, config.OLLAMA_MODEL),
                )
            conn.execute("UPDATE posts SET status = 'ready' WHERE id = %s", (p["id"],))
            log.info("post %s ready (%.2f) with %d drafts", p["id"], s["relevance"], len(comments))
        except Exception:
            # Mark it rather than retry forever. Requeue by hand:
            # UPDATE posts SET status='new' WHERE status='error';
            log.exception("post %s failed", p["id"])
            conn.execute("UPDATE posts SET status = 'error' WHERE id = %s", (p["id"],))


if __name__ == "__main__":
    main()
