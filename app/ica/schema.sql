-- Idempotent schema: every entrypoint runs this on start. Fine for an MVP;
-- swap for real migrations (alembic, sqitch) before the schema starts changing
-- under live data.

CREATE TABLE IF NOT EXISTS sources (
    id               serial PRIMARY KEY,
    kind             text NOT NULL CHECK (kind IN ('handle', 'hashtag')),
    value            text NOT NULL,              -- no leading @ / #, lowercased
    tier             text NOT NULL DEFAULT 'normal' CHECK (tier IN ('hot', 'normal')),
    active           boolean NOT NULL DEFAULT true,
    hashtag_id       text,                       -- cached ig_hashtag_search result
    last_polled      timestamptz,
    last_media_count int,
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, value)
);

-- Every hashtag API call is logged here so the UI can show how many of the
-- 30-unique-hashtags-per-7-days budget is used. Purged after 8 days.
CREATE TABLE IF NOT EXISTS hashtag_queries (
    tag        text NOT NULL,
    queried_at timestamptz NOT NULL DEFAULT now()
);

-- Other accounts' posts. Deleted after RETENTION_DAYS (Meta Platform Terms +
-- GDPR): decisions below survive because they're our own data.
CREATE TABLE IF NOT EXISTS posts (
    id             serial PRIMARY KEY,
    media_id       text NOT NULL UNIQUE,
    source_id      int REFERENCES sources ON DELETE SET NULL,
    owner          text,                         -- null for hashtag results (API omits username)
    caption        text,
    media_type     text,
    permalink      text,
    posted_at      timestamptz,
    like_count     int,
    comments_count int,
    image          bytea,                        -- MVP shortcut; S3 on EKS
    image_type     text,
    status         text NOT NULL DEFAULT 'new',  -- new|skipped|ready|approved|rejected|posted|expired|error
    relevance      real,
    mood           text,
    score_reason   text,
    model          text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS posts_status_idx ON posts (status);

CREATE TABLE IF NOT EXISTS drafts (
    id             serial PRIMARY KEY,
    post_id        int NOT NULL REFERENCES posts ON DELETE CASCADE,
    text           text NOT NULL,
    prompt_version text NOT NULL,
    model          text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- The learning signal. original_text vs final_text is the most valuable pair
-- in the whole system: it's exactly how the model's draft missed your voice.
CREATE TABLE IF NOT EXISTS decisions (
    id            serial PRIMARY KEY,
    post_id       int REFERENCES posts ON DELETE SET NULL,
    draft_id      int REFERENCES drafts ON DELETE SET NULL,
    action        text NOT NULL CHECK (action IN ('approve', 'edit', 'reject')),
    reviewer      text,
    original_text text,
    final_text    text,
    reject_reason text,
    permalink     text,
    mood          text,
    decided_at    timestamptz NOT NULL DEFAULT now(),
    posted_at     timestamptz,
    likes         int,
    replies       int,
    outcome_at    timestamptz
);

-- v2: source categories + stored scoring answers.
-- The three engagement pillars (rules in llm.relevance, approach in llm.STANCES):
-- 'general' = golf content: media/tours, comment when clothing is the topic;
-- 'brand'   = adjacent apparel brands: appreciate their work;
-- 'style'   = influencers/golfers: compliment, only on outfit/trouser posts.
ALTER TABLE sources ADD COLUMN IF NOT EXISTS category text NOT NULL DEFAULT 'general';
-- The model's raw answers behind each score, so a bad score can be traced
-- to the specific question it got wrong.
ALTER TABLE posts ADD COLUMN IF NOT EXISTS signals jsonb;
