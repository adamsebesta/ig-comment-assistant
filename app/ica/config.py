"""All settings come from env vars: the ConfigMap (k8s/local/config.yaml) for
tunables, the Secret (make secrets) for credentials."""
import os


def _int(name, default):
    return int(os.environ.get(name, default))


def _float(name, default):
    return float(os.environ.get(name, default))


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://ica:ica@localhost:5432/ica")

# --- Meta ---
META_TOKEN = os.environ.get("META_TOKEN", "")
IG_USER_ID = os.environ.get("IG_USER_ID", "")
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "")  # empty = app's default version

# --- Ingest pacing ---
# Documented budget is ~200 calls/hour. Ingest runs every 15 min with at most
# MAX_CALLS_PER_RUN calls, so the hard ceiling is 4 x 15 = 60 calls/hour.
MAX_CALLS_PER_RUN = _int("MAX_CALLS_PER_RUN", 15)
PACE_SECONDS = _float("PACE_SECONDS", 10)
USAGE_CEILING = _int("USAGE_CEILING", 75)  # % from X-App-Usage; stop the run above this
HOT_INTERVAL_MIN = _int("HOT_INTERVAL_MIN", 15)
NORMAL_INTERVAL_MIN = _int("NORMAL_INTERVAL_MIN", 360)
HASHTAG_INTERVAL_MIN = _int("HASHTAG_INTERVAL_MIN", 120)  # recent_media only covers 24h
HASHTAG_BUDGET = _int("HASHTAG_BUDGET", 30)
MEDIA_PER_SOURCE = _int("MEDIA_PER_SOURCE", 5)
MAX_POST_AGE_HOURS = _int("MAX_POST_AGE_HOURS", 48)  # older posts aren't worth a comment

# --- Model ---
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5vl:3b")
DRAFT_MODEL = os.environ.get("DRAFT_MODEL", "")  # empty = same as OLLAMA_MODEL
LLM_TIMEOUT = _float("LLM_TIMEOUT", 600)  # CPU inference on kind is slow
LLM_NUM_CTX = _int("LLM_NUM_CTX", 8192)
IMAGE_MAX_PX = _int("IMAGE_MAX_PX", 768)
RELEVANCE_THRESHOLD = _float("RELEVANCE_THRESHOLD", 0.6)
WORKER_BATCH = _int("WORKER_BATCH", 10)
TONE_FILE = os.environ.get("TONE_FILE", "/config/tone.md")

# --- Retention ---
RETENTION_DAYS = _int("RETENTION_DAYS", 90)
