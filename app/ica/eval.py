"""Score the labelled posts in seeds/eval.yaml with the current prompt + model
and report accuracy. Run it before and after every prompt or model change:
without this, prompt tweaks are guesswork.

    make eval                      # current model from the ConfigMap
    make eval MODEL=qwen2.5vl:7b   # compare another model on the same set

Read-only: posts already in the DB are scored from there; others are fetched
via Business Discovery (one GET per handle). Nothing is written.
"""
import os
import re
import sys
from pathlib import Path

import yaml

from . import config, db, llm
from .ingest import pick_image
from .meta import MetaClient

SHORTCODE = re.compile(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")


def code(url):
    m = SHORTCODE.search(url or "")
    return m.group(1) if m else None


def find_post(conn, client, item, cache):
    c = code(item["url"])
    row = conn.execute(
        "SELECT p.*, COALESCE(s.category, 'general') AS category FROM posts p "
        "LEFT JOIN sources s ON s.id = p.source_id WHERE p.permalink LIKE %s",
        (f"%/{c}/%",),
    ).fetchone()
    if row:
        return dict(row)
    handle = (item.get("handle") or "").lstrip("@")
    if not handle or handle == "TODO":
        return None
    if handle not in cache:
        _, cache[handle] = client.business_discovery(handle, 50)
    for m in cache[handle]:
        if code(m.get("permalink")) == c:
            url = pick_image(m)
            image = client.download(url)[0] if url else None
            return {"owner": handle, "caption": m.get("caption"), "media_type": m.get("media_type"),
                    "image": image, "category": item.get("category", "general")}
    return None


def main():
    if os.environ.get("EVAL_MODEL"):
        config.OLLAMA_MODEL = os.environ["EVAL_MODEL"]
    items = yaml.safe_load(sys.stdin) or []
    tone_path = Path(config.TONE_FILE)
    tone = tone_path.read_text() if tone_path.exists() else ""
    conn, client, cache = db.connect(), MetaClient(), {}

    print(f"model={config.OLLAMA_MODEL} prompt={llm.PROMPT_VERSION} threshold={config.RELEVANCE_THRESHOLD}\n")
    right = total = 0
    tp = fp = fn = 0
    for item in items:
        post = find_post(conn, client, item, cache)
        if not post:
            print(f"  SKIP  {item['url']}  (not in DB; needs a valid handle)")
            continue
        category = item.get("category", post["category"])
        s = llm.score(post, tone, category)
        predicted = "relevant" if s["relevance"] >= config.RELEVANCE_THRESHOLD else "not relevant"
        ok = predicted == item["label"]
        right += ok
        total += 1
        tp += predicted == item["label"] == "relevant"
        fp += predicted == "relevant" != item["label"]
        fn += item["label"] == "relevant" != predicted
        sig = s["signals"]
        print(f"  {'PASS' if ok else 'FAIL'}  {s['relevance']:.2f}  want={item['label']:<12} "
              f"[{category}, focus={sig['clothing_focus']}, trousers={sig['trousers_visible']}, "
              f"topics={','.join(sig['topics']) or '-'}]  {item['url']}")
        if not ok:
            print(f"        model saw: {s['reason']}")
    print(f"\n{right}/{total} correct" + (f" ({100 * right / total:.0f}%)" if total else ""))
    # Accuracy alone flatters a model that says "no" to everything, because most
    # posts are irrelevant. Recall: did we catch the posts worth commenting on?
    # Precision: of what we queued, how much was worth it?
    print(f"recall {tp}/{tp + fn} relevant posts caught · precision {tp}/{tp + fp} queued posts were relevant")


if __name__ == "__main__":
    main()
