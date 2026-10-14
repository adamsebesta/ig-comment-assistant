# ig-comment-assistant

Finds Instagram posts relevant to a golf apparel brand, scores them with a
vision-language model, drafts comments that add value (not plugs), and queues them for
human review. **A human posts every comment by hand.** The system only reads, through
the official Graph API.

## Safety rules (non-negotiable)

- Read-only Graph API calls only (`app/ica/meta.py`). Nothing in this repo posts, comments, likes, follows or messages.
- No password logins, unofficial API libraries, scraping or browser automation on Instagram.
- Ingest is paced: ≤5 calls per 15-min run, spaced 10 s apart, and it stops on throttling or when `X-App-Usage` passes 75%.
- Other accounts' posts are deleted after 90 days.

## Flow

```
CronJob ingest (15 min) ─► Business Discovery (seed handles) + Hashtag recent_media
                         ─► posts + images in Postgres
CronJob worker (30 min)  ─► Ollama VLM scores image + caption ─► drafts 3 comments if relevant
web (FastAPI)            ─► queue ─► approve/edit/reject ─► copy + open post ─► mark posted ─► enter likes/replies
```

## Local MVP quickstart

Needs: Docker Desktop, kind, kubectl, and [Ollama](https://ollama.com) running natively on the Mac.
The models run on the Mac's GPU (Metal), not in kind: Docker can't pass the Apple GPU into
containers, and CPU inference inside Docker's VM took minutes per post and starved the host.

```bash
brew install ollama && brew services start ollama
ollama pull qwen2.5vl:7b     # scoring (vision)
ollama pull qwen3:14b        # drafting (text)
# optional: one model in memory at a time
#   add OLLAMA_MAX_LOADED_MODELS=1 to ~/Library/LaunchAgents/sh.brew.ollama.plist
cp .env.example .env              # fill in META_TOKEN (and app id/secret for long-token)
make bootstrap                    # cluster, image, secrets, tone, manifests
make ui                           # http://localhost:8000 → Sources → paste seeds/handles.example.txt
make ingest-now && make logs      # don't wait for the schedule
make worker-now && make logs      # scoring on CPU is slow: ~30-60 s per post
```

After code changes: `make rollout` (rebuild + restart web). CronJobs pick up the new image on their next run.

The Explorer token expires in ~1 hour. Run `make long-token`, paste the result into `.env`, then `make secrets`.

## Layout

```
app/ica/          meta.py (read-only Graph client) · ingest.py · llm.py · worker.py · web.py · schema.sql
k8s/local/        namespace, config, postgres, ollama, web, cronjobs
local/            kind config
scripts/          bd_probe.py: the step-zero Business Discovery probe
seeds/            *.example.* committed; your real handles/hashtags/tone.md are gitignored
```

## Known limits

- Reels never return a video URL through Business Discovery, only the cover frame. Scoring sees one still + the caption.
- Hashtag search is **untested**: it may need the "Instagram Public Content Access" feature enabled on the app. Try one hashtag before adding more.
- The API can't read comments on other accounts' posts, so outcomes (likes/replies on our comments) are entered by hand on the Posted page.

## Not yet built

Daily digest notification · hot-tier phone alert · learned ranker (needs ~200 decisions) · EKS (Terraform from eks-ai-playground, RDS, S3, Argo Workflows, vLLM on Karpenter GPU).
