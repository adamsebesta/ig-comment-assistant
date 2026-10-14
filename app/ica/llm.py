"""Scoring and drafting through Ollama's chat API.

Ollama locally, vLLM on EKS. Both speak a chat API with image input, so only
chat() should need to change when we move. `format` = a JSON schema, which
makes the model return parseable JSON instead of prose we'd have to regex.
"""
import base64
import io
import json
import re

import httpx
from PIL import Image

from . import config

PROMPT_VERSION = "v13"  # bump when prompts change, so decisions can be compared per version

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
exclusion: "competitor_ad" if a clothing brand is promoting its own product: an advert, a
  campaign shot, a collection launch, "shop now", a celebrity or model wearing the brand for it.
  "sensitive" if it involves injury, death, politics or tragedy. Otherwise "none".
audience: whose clothes are shown: "men", "women", "mixed", or "none".
clothing_debate: true ONLY if the post is about people reacting to or debating a clothing or
  style choice: a notable or controversial outfit, a dress-code argument, a "fashion moment",
  a style change people are talking about. A tournament, a shot or an interview is NOT this,
  even if someone wears nice clothes.
looks_ai_generated: true if the person or image looks AI-generated or CGI: waxy or too-perfect
  skin, impossible details, a synthetic studio look, a fake magazine cover, a virtual influencer.
mood: the post's mood.
reason: one short sentence describing what the post shows."""

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "clothing_focus": {"type": "string", "enum": ["central", "incidental", "none"]},
        "trousers_visible": {"type": "boolean"},
        "topics": {"type": "array", "items": {"type": "string", "enum": TOPICS}},
        "exclusion": {"type": "string", "enum": ["none", "competitor_ad", "sensitive"]},
        "audience": {"type": "string", "enum": ["men", "women", "mixed", "none"]},
        "clothing_debate": {"type": "boolean"},
        "looks_ai_generated": {"type": "boolean"},
        "mood": {"type": "string", "enum": MOODS},
        "reason": {"type": "string"},
    },
    "required": ["clothing_focus", "trousers_visible", "topics", "exclusion", "audience",
                 "clothing_debate", "looks_ai_generated", "mood", "reason"],
}


CATEGORIES = ("general", "brand", "style")


# Captions often admit it even when the image is convincing. Checked in code,
# because a vision model's AI-image judgement is unreliable on its own.
AI_CAPTION = re.compile(r"(?i)(#ai(art|generated|model|influencer|fashion|photography|girl|beauty)?\b|\bai[- ]generated\b|\bvirtual (influencer|model)\b|"
                        r"\bmidjourney\b|\bstable diffusion\b|\bmade with ai\b|\bai (model|art|fashion)\b)")


# Hashtag search returns no cover frame for videos, so many posts are caption-only,
# and a 7B vision model under-weights text when it expects an image. These caption
# checks make the trouser and advert signals deterministic.
TROUSER_CAPTION = re.compile(r"(?i)\b(trousers?|pants|slacks|chinos?|corduroys?|cords|pleat(s|ed)?|"
                             r"high[- ]waist(ed)?|wide[- ]leg|straight[- ]leg|turn[- ]?ups?|drape[sd]?)\b")
# The model over-calls "clothing_debate" (it flagged a club-selection interview), so a
# debate only counts if the caption itself talks about clothes or style.
CLOTHING_CAPTION = re.compile(r"(?i)\b(style|stylish|outfits?|look(s|ed)?|fit(s)?|wore|wears?|wearing|dress(ed)?|"
                              r"dress[- ]code|fashion|kit|attire|shirts?|polos?|mock[- ]?necks?|sweaters?|"
                              r"knit(wear)?|jackets?|shoes|trousers?|pants|shorts|caps?|hats?|threads|drip)\b")
AD_CAPTION = re.compile(r"(?i)(\b(our|the) (next|new|latest) (collection|drop|release)\b|\bshop (now|the look)\b|"
                        r"\blink in (bio|profile)\b|\bpre-?order\b|\buse code\b|\bavailable now\b|"
                        r"\bdrops? (on|this|today|tomorrow|friday|monday)\b|\bnew arrivals?\b|% off\b)")


def relevance(sig, category):
    """The scoring rule. Threshold is RELEVANCE_THRESHOLD (0.6): only clothing-central
    posts can pass, in all three pillars.
      general: golf media. Clothing must be the subject (a tour pro's new trousers), not a backdrop.
      brand:   adjacent apparel brands. Their posts ARE product adverts, so the
               competitor_ad exclusion doesn't apply; we're there to appreciate the work.
      style:   creators/golfers. Trousers or a full outfit must be on show."""
    exclusion = sig["exclusion"]
    if sig.get("ai_generated") or sig.get("looks_ai_generated"):
        return 0.0  # never engage with AI-generated people/accounts
    if exclusion == "sensitive" or (exclusion == "competitor_ad" and category != "brand"):
        return 0.0
    if sig.get("audience") == "women":
        return 0.1  # menswear brand: womenswear-only posts aren't our conversation
    n = len(set(sig["topics"]))
    central = sig["clothing_focus"] == "central"
    # Trouser-centred brand: every pillar needs trousers on show or under discussion.
    trousers = sig["trousers_visible"] or "trousers_and_fit" in sig["topics"]
    # Style debates ("the day golf purists lost their minds" over Tiger's mock-neck) are
    # great engagement even when trousers aren't the subject.
    if sig.get("clothing_debate") and sig["clothing_focus"] != "none":
        return 0.7
    if not central:
        return 0.1 if category == "style" else min(0.5, 0.2 + 0.1 * n) if sig["clothing_focus"] == "incidental" else 0.0
    if not trousers:
        return 0.4  # clothing post, but not about trousers: below the 0.6 threshold
    base = {"style": 0.7, "brand": 0.7}.get(category, 0.6)
    return min(1.0, base + 0.1 * n)


STANCES = {
    "general": "This is a golf media or brand account. Add to the conversation with one "
               "specific insight; a compliment is fine if it's specific.",
    "brand": "This is a fellow golf apparel brand we respect. Appreciate their work "
             "specifically (the cut, the cloth, the styling, the photography), peer to peer. "
             "Never compare it to ours, never compete, never hint at our own products.",
    "style": "This is a style creator or golfer whose taste we admire. Lead with a genuine, specific "
             "compliment on the outfit or trousers, peer to peer, then optionally one insight.",
}

# Which registers to draft, per pillar. Named JSON fields (not "3 comments")
# force three genuinely different comments instead of three rewordings.
REGISTERS = {
    "general": ["punchy", "insightful", "contemplative"],
    "brand": ["compliment", "punchy", "insightful"],
    "style": ["compliment", "punchy", "insightful"],
}

DRAFT_RULES = """
Write Instagram comments for the brand account to post on this post, one per register
named below, as described in the Voice section above.
Rules:
- Be specific: name something actually in this post (a garment, a colour, a cloth, the setting,
  a named item from the caption). A comment that could sit under any post is a failure.
- Add value: an observation, a specific compliment, a detail about cut, cloth or fit, or a genuine question.
- Never mention the brand, never sell, no links, at most one emoji, and only one that fits golf or clothing.
- No hashtags, unless the Voice section allows a signature tag.
- Write how a real person types an Instagram comment: 2 to 12 words, casual, fragments are
  fine, lowercase is fine. Not a polished sentence, not a caption, not a review.
- No em dashes, no semicolons, no filler phrases.
- Carry the brand's beliefs in your own fresh words. Never copy phrases from the brand
  context, the example comments, the caption or our recent comments.
- Match the post's mood. In a debate, take a considered position without being combative."""

DRAFT_ATTEMPTS = 4
MAX_DRAFT_CHARS = 70  # hard ceiling; real comments are short (ours: 12-65 chars)
NGRAM = 4  # sharing this many consecutive words with the tone file = verbatim copy
# Short example comments are templates the model loves to reword ("relaxed fit,
# relaxed vibe"), so against those a 3-word overlap already counts as a copy.
EXAMPLE_NGRAM = 3
CLICHES = re.compile(r"(?i)\b(game[- ]changer|seamless(ly)?|elevate[sd]?|next level|on point|"
                     r"effortless(ly)?|timeless elegance|must[- ]have)\b")
# Emoji the brand would actually use. The 7B model sprinkles random ones (🛌 🕳️ 🎟).
ALLOWED_EMOJI = set("👖⛳🎯🔥💯👌👏🙌👀✨🔮🍂🤝💪") | {"🏌️", "🏌️‍♂️", "🏌"}
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF][\uFE0F\u200D\u2640\u2642\U0001F3FB-\U0001F3FF]*")
OFF_TOPIC_EMOJI = re.compile("[\U0001F3BE\U0001F3C0\U0001F3C8\U0001F3D0\u26BD\u26BE\U0001F3D2\U0001F3D3\U0001F3F8\U0001F94E]")


def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def _ngrams(text, n=NGRAM):
    w = _words(text)
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def _norm(text):
    return " ".join(_words(text))


def tidy(text):
    """Deterministic fixes instead of retries. The model reaches for em dashes no
    matter what it's told (the classic tell of machine-written text); people type ' - '."""
    text = text.strip().strip('"').strip()
    text = re.sub(r"\s*[\u2014\u2013]\s*", " - ", text)
    return re.sub(r"\s{2,}", " ", text)


def check_draft(text, corpus_ngrams, corpus_lines=frozenset()):
    """Returns why a draft is rejected, or None. Enforced in code because a
    prompt can only ask: "never verbatim" has to be checked, not hoped for.
    corpus_lines catches exact copies too short for n-grams ("Super solid!"):
    the same comment on many posts is exactly what spam detection looks for."""
    if _norm(text) in corpus_lines:
        return "exact copy of an example or recent comment"
    if len(text) > MAX_DRAFT_CHARS:
        return f"too long ({len(text)} chars)"
    if ";" in text:
        return "semicolon"
    if len(re.findall(r"[.!?]+(?=\s|$)", text)) > 2:
        return "more than 2 sentences"
    # Corpus holds 4-grams (whole tone file) and 3-grams (short examples); match both.
    copied = (_ngrams(text) | _ngrams(text, EXAMPLE_NGRAM)) & corpus_ngrams
    if copied:
        return "copies '" + " ".join(next(iter(copied))) + "'"
    if re.search(r"(?i)\b(sale|discount|shop|link in bio|dm us)\b", text):
        return "promotional language"
    if m := CLICHES.search(text):
        return f"cliché '{m.group(0)}'"
    if OFF_TOPIC_EMOJI.search(text):
        return "emoji from another sport"
    odd = [e for e in EMOJI.findall(text) if e not in ALLOWED_EMOJI and e.rstrip("\uFE0F") not in ALLOWED_EMOJI]
    if odd:
        return f"emoji {odd[0]} doesn't fit"
    return None


def chat(messages, schema, temperature, model=None):
    r = httpx.post(
        f"{config.OLLAMA_URL}/api/chat",
        json={
            "model": model or config.OLLAMA_MODEL,
            "think": False,  # reasoning models (qwen3) otherwise spend tokens thinking first
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


def _post_message(post, extra="", image=True):
    text = (
        f"Account: @{post['owner'] or 'unknown (hashtag result)'}\n"
        f"Type: {post['media_type']}"
        + (" (only the cover frame is shown)" if post["media_type"] == "VIDEO" else "")
        + f"\nCaption:\n{post['caption'] or '(none)'}{extra}"
    )
    msg = {"role": "user", "content": text}
    if image and post["image"]:
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
    caption = post["caption"] or ""
    sig["ai_generated"] = bool(sig.get("looks_ai_generated") or AI_CAPTION.search(caption))
    if TROUSER_CAPTION.search(caption):
        sig["caption_trousers"] = True
        if "trousers_and_fit" not in sig["topics"]:
            sig["topics"].append("trousers_and_fit")
        if not post["image"] and sig["clothing_focus"] != "central":
            sig["clothing_focus"] = "central"  # caption-only post about trousers: the caption IS the subject
    if sig.get("clothing_debate") and not CLOTHING_CAPTION.search(caption):
        sig["clothing_debate"] = False
        sig["debate_overruled"] = True  # model said debate, caption isn't about clothes
    if AD_CAPTION.search(caption) and sig["exclusion"] == "none":
        sig["exclusion"] = "competitor_ad"
        sig["caption_ad"] = True
    return {"relevance": relevance(sig, category), "mood": sig["mood"], "reason": sig["reason"], "signals": sig}


def draft(post, scored, tone, recent, category="general"):
    """Returns (accepted drafts, rejected [(text, reason)]). Up to DRAFT_ATTEMPTS
    rounds; each retry is told exactly what was rejected and why. Cheap on a GPU
    (~2-3 s per round), and the model reliably over-writes on the first try."""
    registers = REGISTERS.get(category, REGISTERS["general"])
    schema = {"type": "object", "properties": {r: {"type": "string"} for r in registers},
              "required": registers}
    recent_block = "\n".join(f"- {t}" for t in recent) or "(none yet)"
    corpus = _ngrams(tone) | _ngrams(post["caption"] or "")
    for t in recent:
        corpus |= _ngrams(t)
    examples = [l.lstrip("-* ").strip() for l in tone.split("# Example comments", 1)[-1].splitlines()]
    examples += re.findall(r'"([^"]+)"', tone.split("# Never", 1)[0]) + list(recent)
    for e in examples:
        corpus |= _ngrams(e, EXAMPLE_NGRAM)
    lines = {_norm(l.lstrip("-* ").strip('"')) for l in tone.splitlines() + list(recent)}
    lines |= {_norm(q) for q in re.findall(r'"([^"]+)"', tone)}
    lines.discard("")

    accepted, rejected, feedback = {}, [], ""
    for _attempt in range(DRAFT_ATTEMPTS):
        missing = [r for r in registers if r not in accepted]
        extra = (
            f"\n\nApproach: {STANCES.get(category, STANCES['general'])}"
            f"\nMood: {scored['mood']}\nWhat the post shows: {scored['reason']}"
            f"\nRegisters to write: {', '.join(missing)}"
            f"\n\nRecent comments we've posted (don't repeat them):\n{recent_block}{feedback}"
        )
        # The drafting model can be a stronger text-only model (DRAFT_MODEL): scoring
        # needs vision, writing doesn't, since "What the post shows" already describes it.
        draft_model = config.DRAFT_MODEL or config.OLLAMA_MODEL
        out = chat(
            [{"role": "system", "content": tone + "\n" + DRAFT_RULES},
             _post_message(post, extra, image=draft_model == config.OLLAMA_MODEL)],
            {**schema, "properties": {r: schema["properties"][r] for r in missing}, "required": missing},
            0.8, model=draft_model,
        )
        notes = []
        for r in missing:
            text = tidy(out.get(r) or "")
            why = check_draft(text, corpus, lines) if text else "empty"
            if why:
                rejected.append((text, why))
                notes.append(f"- your {r} draft \"{text}\" was rejected: {why}")
            else:
                accepted[r] = text
        if len(accepted) == len(registers):
            break
        feedback = ("\n\nRewrite these, fixing the problem. Shorter: a few casual words, "
                    "like a real person typing on their phone:\n" + "\n".join(notes))
    return [accepted[r] for r in registers if r in accepted], rejected
