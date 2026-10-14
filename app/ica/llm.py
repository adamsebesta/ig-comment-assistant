"""Scoring and drafting through Ollama's chat API.

Ollama locally, vLLM on EKS. Both speak a chat API with image input, so only
chat() should need to change when we move. `format` = a JSON schema, which
makes the model return parseable JSON instead of prose we'd have to regex.
"""
import base64
import io
import json

import httpx
from PIL import Image

from . import config

PROMPT_VERSION = "v5"  # bump when prompts change, so decisions can be compared per version

MOODS = ["celebratory", "debate", "product", "news", "instructional", "humour", "other"]

# Brand specifics (who we are, what we care about, voice) live in the private
# tone file (seeds/tone.md -> ConfigMap), which is prepended to both prompts.
# These rubrics stay brand-agnostic so the repo can be public.
# Small models are poor at holistic 0-1 scores (they snap to 0 or 1). Instead
# the model answers concrete questions and relevance() computes the score, so
# the rule is readable, tunable, and a wrong score points at a wrong answer.
TOPICS = ["trousers_and_fit", "full_outfit", "cloth_and_fibre", "craft_and_details",
          "style_change_or_debate", "classic_style", "course_to_dinner"]

SCORE_PROMPT = """Answer questions about this Instagram post for the brand described above.

clothing_focus: are the clothes a main subject of the post?
  "central": the post is about what someone is wearing: an outfit or fit shot, a style
    change, a look being shown off, a discussion of clothes or fit.
  "incidental": clothes are visible, but the post is about something else (a shot, a win,
    a course, a joke).
  "none": no person or clothing to speak of.
trousers_visible: can you clearly see a person's trousers in the image? true or false.
topics: which of these the post actually touches (can be empty): trousers_and_fit,
  full_outfit, cloth_and_fibre, craft_and_details, style_change_or_debate, classic_style,
  course_to_dinner.
exclusion: "competitor_ad" if it is an advert for another clothing brand's product,
  "sensitive" if it involves injury, death, politics or tragedy, otherwise "none".
mood: the post's mood.
reason: one short sentence describing what the post shows."""

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "clothing_focus": {"type": "string", "enum": ["central", "incidental", "none"]},
        "trousers_visible": {"type": "boolean"},
        "topics": {"type": "array", "items": {"type": "string", "enum": TOPICS}},
        "exclusion": {"type": "string", "enum": ["none", "competitor_ad", "sensitive"]},
        "mood": {"type": "string", "enum": MOODS},
        "reason": {"type": "string"},
    },
    "required": ["clothing_focus", "trousers_visible", "topics", "exclusion", "mood", "reason"],
}


CATEGORIES = ("general", "brand", "style")


def relevance(sig, category):
    """The scoring rule. Threshold is RELEVANCE_THRESHOLD (0.6): only clothing-central
    posts can pass, in all three pillars.
      general: golf media. Clothing must be the subject (a tour pro's new trousers), not a backdrop.
      brand:   adjacent apparel brands. Their posts ARE product adverts, so the
               competitor_ad exclusion doesn't apply; we're there to appreciate the work.
      style:   creators/golfers. Trousers or a full outfit must be on show."""
    exclusion = sig["exclusion"]
    if exclusion == "sensitive" or (exclusion == "competitor_ad" and category != "brand"):
        return 0.0
    n = len(set(sig["topics"]))
    central = sig["clothing_focus"] == "central"
    if category == "style":
        if not central:
            return 0.1
        if not (sig["trousers_visible"] or "full_outfit" in sig["topics"]):
            return 0.3
        return min(1.0, 0.7 + 0.1 * n)
    if central:
        return min(1.0, (0.7 if category == "brand" else 0.6) + 0.1 * n)
    if sig["clothing_focus"] == "incidental":
        return min(0.5, 0.2 + 0.1 * n)
    return 0.0


STANCES = {
    "general": "This is a golf media or brand account. Add to the conversation with one "
               "specific insight; a compliment is fine if it's specific.",
    "brand": "This is a fellow golf apparel brand we respect. Appreciate their work "
             "specifically (the cut, the cloth, the styling, the photography), peer to peer. "
             "Never compare it to ours, never compete, never hint at our own products.",
    "style": "This is a style creator or golfer whose taste we admire. Lead with a genuine, specific "
             "compliment on the outfit or trousers, peer to peer, then optionally one insight.",
}

DRAFT_RULES = """
Write 3 distinct Instagram comments for the brand account to post on this post.
Rules:
- Add value to the conversation: an observation, a detail about cut, cloth or fit, a bit of golf history, or a genuine question.
- Never mention the brand, never sell, no links, at most one emoji.
- No hashtags, unless the Voice section above allows a signature tag.
- If the Voice section defines registers, write one comment in each.
- Follow the Voice and Never sections above exactly.
- Under 200 characters each. Sound like a knowledgeable person, not a marketer.
- Match the post's mood. In a debate, take a considered position without being combative.
- Don't reuse phrasing from the recent comments listed."""

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"comments": {"type": "array", "items": {"type": "string"}}},
    "required": ["comments"],
}


def chat(messages, schema, temperature):
    r = httpx.post(
        f"{config.OLLAMA_URL}/api/chat",
        json={
            "model": config.OLLAMA_MODEL,
            "messages": messages,
            "stream": False,
            "format": schema,
            # Ollama's default context (~4k tokens) silently truncates the start of
            # the prompt (our brand context) once an image is attached. Set it explicitly.
            "options": {"temperature": temperature, "num_ctx": config.LLM_NUM_CTX},
        },
        timeout=config.LLM_TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Ollama {r.status_code}: {r.text[:300]}")
    return json.loads(r.json()["message"]["content"])


def _post_message(post, extra=""):
    text = (
        f"Account: @{post['owner'] or 'unknown (hashtag result)'}\n"
        f"Type: {post['media_type']}"
        + (" (only the cover frame is shown)" if post["media_type"] == "VIDEO" else "")
        + f"\nCaption:\n{post['caption'] or '(none)'}{extra}"
    )
    msg = {"role": "user", "content": text}
    if post["image"]:
        msg["images"] = [base64.b64encode(_shrink(bytes(post["image"]))).decode()]
    return msg


def _shrink(raw):
    """Downscale before sending. Qwen-VL's token count grows with pixel area: a
    1080x1920 frame is ~3.5k tokens, 768px max side is ~600. Scoring doesn't
    need full resolution, and CPU inference time drops with it."""
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img.thumbnail((config.IMAGE_MAX_PX, config.IMAGE_MAX_PX))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


def score(post, tone="", category="general"):
    sig = chat([{"role": "system", "content": tone + "\n\n" + SCORE_PROMPT}, _post_message(post)], SCORE_SCHEMA, 0)
    sig["topics"] = [t for t in sig["topics"] if t in TOPICS]
    return {"relevance": relevance(sig, category), "mood": sig["mood"], "reason": sig["reason"], "signals": sig}


def draft(post, scored, tone, recent, category="general"):
    recent_block = "\n".join(f"- {t}" for t in recent) or "(none yet)"
    extra = (
        f"\n\nApproach: {STANCES.get(category, STANCES['general'])}"
        f"\nMood: {scored['mood']}\nWhat the post shows: {scored['reason']}"
        f"\n\nRecent comments we've posted:\n{recent_block}"
    )
    out = chat(
        [{"role": "system", "content": tone + "\n" + DRAFT_RULES}, _post_message(post, extra)],
        DRAFT_SCHEMA, 0.8,
    )
    return [c.strip() for c in out["comments"] if c.strip()][:3]
