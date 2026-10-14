"""Review UI: queue -> approve/edit/reject -> copy + open post -> mark posted -> outcomes.

No auth: locally it's only reachable via `make ui` (port-forward). On EKS it
sits behind ALB OIDC (Cognito); never expose this Service without that.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import config, db, llm

templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))


@asynccontextmanager
async def lifespan(_):
    with db.connect() as conn:
        db.init(conn)
    yield


app = FastAPI(lifespan=lifespan)


def back(url):
    return RedirectResponse(url, status_code=303)  # 303: POST -> GET after a form submit


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def queue(request: Request):
    with db.connect() as conn:
        posts = conn.execute(
            "SELECT p.id, owner, caption, media_type, permalink, posted_at, like_count, "
            "comments_count, relevance, mood, score_reason, signals, image IS NOT NULL AS has_image, "
            "extract(epoch FROM now() - posted_at)::int AS age_s, "
            "COALESCE(s.category, 'general') AS category "
            "FROM posts p LEFT JOIN sources s ON s.id = p.source_id "
            # Newest first: comments posted early sit at the top of a post's thread.
            "WHERE status = 'ready' ORDER BY posted_at DESC"
        ).fetchall()
        for p in posts:
            p["drafts"] = conn.execute(
                "SELECT id, text FROM drafts WHERE post_id = %s ORDER BY id", (p["id"],)
            ).fetchall()
    return templates.TemplateResponse(request, "queue.html", {"posts": posts})


@app.get("/image/{post_id}")
def image(post_id: int):
    with db.connect() as conn:
        row = conn.execute("SELECT image, image_type FROM posts WHERE id = %s", (post_id,)).fetchone()
    if not row or not row["image"]:
        return Response(status_code=404)
    return Response(content=bytes(row["image"]), media_type=row["image_type"] or "image/jpeg")


@app.post("/posts/{post_id}/approve")
def approve(post_id: int, draft_id: int = Form(...), text: str = Form(...), reviewer: str = Form("")):
    with db.connect() as conn:
        d = conn.execute("SELECT text FROM drafts WHERE id = %s", (draft_id,)).fetchone()
        p = conn.execute("SELECT permalink, mood FROM posts WHERE id = %s", (post_id,)).fetchone()
        action = "approve" if text.strip() == d["text"].strip() else "edit"
        conn.execute(
            "INSERT INTO decisions (post_id, draft_id, action, reviewer, original_text, final_text, permalink, mood) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (post_id, draft_id, action, reviewer or None, d["text"], text.strip(), p["permalink"], p["mood"]),
        )
        conn.execute("UPDATE posts SET status = 'approved' WHERE id = %s", (post_id,))
    return back("/approved")


@app.post("/posts/{post_id}/reject")
def reject(post_id: int, reason: str = Form(""), reviewer: str = Form("")):
    with db.connect() as conn:
        p = conn.execute("SELECT permalink, mood FROM posts WHERE id = %s", (post_id,)).fetchone()
        conn.execute(
            "INSERT INTO decisions (post_id, action, reviewer, reject_reason, permalink, mood) "
            "VALUES (%s, 'reject', %s, %s, %s, %s)",
            (post_id, reviewer or None, reason or None, p["permalink"], p["mood"]),
        )
        conn.execute("UPDATE posts SET status = 'rejected' WHERE id = %s", (post_id,))
    return back("/")


@app.get("/approved")
def approved(request: Request):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, final_text, permalink, decided_at FROM decisions "
            "WHERE action IN ('approve', 'edit') AND posted_at IS NULL ORDER BY decided_at"
        ).fetchall()
    return templates.TemplateResponse(request, "approved.html", {"rows": rows})


@app.post("/decisions/{decision_id}/posted")
def mark_posted(decision_id: int):
    with db.connect() as conn:
        conn.execute("UPDATE decisions SET posted_at = now() WHERE id = %s", (decision_id,))
        conn.execute(
            "UPDATE posts SET status = 'posted' WHERE id = (SELECT post_id FROM decisions WHERE id = %s)",
            (decision_id,),
        )
    return back("/approved")


@app.get("/posted")
def posted(request: Request):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, final_text, permalink, posted_at, likes, replies FROM decisions "
            "WHERE posted_at IS NOT NULL ORDER BY posted_at DESC LIMIT 100"
        ).fetchall()
    return templates.TemplateResponse(request, "posted.html", {"rows": rows})


@app.post("/decisions/{decision_id}/outcome")
def outcome(decision_id: int, likes: int = Form(...), replies: int = Form(...)):
    with db.connect() as conn:
        conn.execute(
            "UPDATE decisions SET likes = %s, replies = %s, outcome_at = now() WHERE id = %s",
            (likes, replies, decision_id),
        )
    return back("/posted")


@app.get("/sources")
def sources(request: Request):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT s.*, count(p.id) FILTER (WHERE (p.signals->>'ai_generated')::boolean) AS ai_posts "
            "FROM sources s LEFT JOIN posts p ON p.source_id = s.id "
            "GROUP BY s.id ORDER BY s.kind, s.category, s.tier, s.value"
        ).fetchall()
        used = db.hashtag_budget_used(conn)
    return templates.TemplateResponse(
        request, "sources.html", {"rows": rows, "used": used, "budget": config.HASHTAG_BUDGET}
    )


@app.post("/sources")
def add_sources(kind: str = Form(...), values: str = Form(...), tier: str = Form("normal"),
                category: str = Form("general")):
    if category not in llm.CATEGORIES:
        category = "general"
    # One per line or comma-separated, so a whole seed file can be pasted in.
    cleaned = {v.strip().lstrip("@#").lower() for v in values.replace(",", "\n").splitlines()}
    with db.connect() as conn:
        for v in filter(None, cleaned):
            conn.execute(
                "INSERT INTO sources (kind, value, tier, category) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (kind, value) DO UPDATE SET tier = EXCLUDED.tier, "
                "category = EXCLUDED.category, active = true",
                (kind, v, tier, category),
            )
    return back("/sources")


@app.post("/sources/{source_id}/toggle")
def toggle_source(source_id: int):
    with db.connect() as conn:
        conn.execute("UPDATE sources SET active = NOT active WHERE id = %s", (source_id,))
    return back("/sources")


@app.post("/sources/{source_id}/delete")
def delete_source(source_id: int):
    # Posts already ingested from it stay (posts.source_id -> NULL) so any
    # pending review or past decision isn't lost; they age out with retention.
    with db.connect() as conn:
        conn.execute("DELETE FROM sources WHERE id = %s", (source_id,))
    return back("/sources")


@app.get("/activity")
def activity(request: Request):
    with db.connect() as conn:
        runs = conn.execute(
            "SELECT job, started_at, finished_at, summary, error, "
            "extract(epoch FROM coalesce(finished_at, now()) - started_at)::int AS secs "
            "FROM runs ORDER BY started_at DESC LIMIT 40"
        ).fetchall()
        statuses = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) AS n FROM posts GROUP BY 1")}
        sources = conn.execute(
            "SELECT count(*) FILTER (WHERE active) AS active, "
            "count(*) FILTER (WHERE active AND last_polled IS NULL) AS never, "
            "count(*) FILTER (WHERE last_error IS NOT NULL) AS failing FROM sources"
        ).fetchone()
        new_24h = conn.execute(
            "SELECT count(*) AS n FROM posts WHERE created_at > now() - interval '24 hours'"
        ).fetchone()["n"]
    return templates.TemplateResponse(request, "activity.html", {
        "runs": runs, "statuses": statuses, "sources": sources, "new_24h": new_24h,
        "model": config.OLLAMA_MODEL,
    })
