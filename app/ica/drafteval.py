"""Generate drafts for the relevant posts in seeds/eval.yaml and print them with
any rejections, without writing anything. For comparing prompt versions by eye:

    make draft-eval
"""
import os
import sys
from pathlib import Path

import yaml

from . import config, db, llm
from .eval import find_post
from .meta import MetaClient


def main():
    if os.environ.get("EVAL_MODEL"):
        config.OLLAMA_MODEL = os.environ["EVAL_MODEL"]
    if os.environ.get("EVAL_DRAFT_MODEL"):
        config.DRAFT_MODEL = os.environ["EVAL_DRAFT_MODEL"]
    items = [i for i in (yaml.safe_load(sys.stdin) or []) if i["label"] == "relevant"]
    tone_path = Path(config.TONE_FILE)
    tone = tone_path.read_text() if tone_path.exists() else ""
    conn, client, cache = db.connect(), MetaClient(), {}
    print(f"score model={config.OLLAMA_MODEL} draft model={config.DRAFT_MODEL or config.OLLAMA_MODEL} prompt={llm.PROMPT_VERSION}\n")
    for item in items:
        post = find_post(conn, client, item, cache)
        if not post:
            continue
        category = item.get("category", post["category"])
        scored = llm.score(post, tone, category)
        drafts, rejected = llm.draft(post, scored, tone, [], category)
        print(f"@{post['owner']} [{category}] {item['url']}")
        if item.get("our_comment"):
            print(f"  ours:     {item['our_comment']}")
        for d in drafts:
            print(f"  draft:    {d}  ({len(d)})")
        for text, why in rejected:
            print(f"  rejected: {text}  <- {why}")
        print()


if __name__ == "__main__":
    main()
