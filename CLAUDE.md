# CLAUDE.md

Semi-automated Instagram comment assistant for a golf apparel brand.
Sister project to `~/Desktop/code/eks-ai-playground`. It reuses that repo's EKS/Karpenter/
Argo CD patterns, and the same learning-first working style applies: Adam is moving from
ECS/Terraform into Kubernetes and GPU infra, so explain the *why* (especially vs. ECS)
and help him diagnose rather than just fix.

## Hard rules: account safety

- **Never add code that writes to Instagram**: no POST to the Graph API, no comment/like/follow/DM
  endpoints, no instagrapi or similar, no browser automation, no scraping. A human posts every comment.
- Keep ingest pacing (`MAX_CALLS_PER_RUN`, `PACE_SECONDS`, `USAGE_CEILING`) conservative. Don't
  raise limits or add extra Meta apps/accounts to get around them (forbidden by the Platform Terms).
- Keep the 90-day retention purge on other accounts' data.

## Facts verified against the live API (2026-10-14)

- Business Discovery returns caption, permalink, timestamp, like/comment counts, image `media_url`
  and carousel children. Videos return **no** `media_url`, only `thumbnail_url`.
- Requires Instagram API **with Facebook Login** + a token with `business_management`, because the
  Page sits in a business portfolio. Without it, `me/accounts` returns empty.
- Hashtag search works (2026-10-14): `ig_hashtag_search` + `recent_media` with 5 posts + children
  returned fresh posts with images (~4% of hourly usage per call). 25 posts + children failed with
  "reduce the amount of data". Busy tags (#golffashion) produce ~5 posts per 30 min.
- X-App-Usage cost scales with response size, not call count: 1-post Business Discovery calls
  cost ~0.25%, 5-post calls with children ~2-3%.

## Runtime facts (2026-10-14)

- Models run in Ollama natively on the Mac (Metal), reached from Pods via a selector-less
  `ollama` Service + hand-written EndpointSlice (192.168.65.254). Use the full DNS name
  `ollama.ica.svc.cluster.local`: Ollama rejects Host headers that aren't localhost/IP/*.local.
  Scoring qwen2.5vl:7b (3B failed eval), drafting qwen3:14b (text-only, clearly better drafts).
- Argo CD syncs `k8s/local/` from GitHub main with selfHeal: change YAML + push, never kubectl edit.
  `make rollout` is still needed for code (Argo doesn't build images).
- Meta token is a system-user token (Employee role, read-only scopes). Expires ~2026-12-13:
  regenerate in Business Settings → System users → comment-assistant → Generate token.
- Hashtag results never include the owner or a video cover frame: hashtag Reels are caption-only,
  and follower counts can't be checked (engagement floor is the proxy).

## Conventions

- Plain YAML manifests in `k8s/local/` for now; Helm/Argo CD when this moves to EKS.
- One image (`app/`), three entrypoints: web, `ica.ingest`, `ica.worker`.
- Brand data (`seeds/*` except examples, `.env`) is gitignored. Keep it that way: the repo is
  public, so no brand name, positioning, account ids, target lists or real comments in tracked files.
