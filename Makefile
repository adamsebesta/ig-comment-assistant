.PHONY: bootstrap up down build ns secrets tone deploy model rollout ui ingest-now worker-now logs psql probe long-token argocd argocd-repo argocd-app argocd-password argocd-ui eval draft-eval

CLUSTER ?= ica
NS      ?= ica
IMAGE   ?= ica:dev
MODEL   ?= qwen2.5vl:7b
# Pinned context: never act on whichever cluster happens to be current (e.g. a
# leftover EKS context). `kind create cluster` names its context kind-<name>.
KUBECTL = kubectl --context kind-$(CLUSTER)
TONE    ?= $(if $(wildcard seeds/tone.md),seeds/tone.md,seeds/tone.example.md)

# Loads .env into each recipe's shell. Secrets stay out of the Makefile and
# out of shell history.
LOAD_ENV = set -a; . ./.env; set +a

## ----- Local cluster (kind) -----

# Everything from zero. Model pull is last because it's the slow bit (~3 GB).
bootstrap: up build ns secrets tone deploy model

up:
	kind create cluster --name $(CLUSTER) --config local/kind-config.yaml

down:
	kind delete cluster --name $(CLUSTER)

# kind nodes can't see your local Docker images; `kind load` copies the image
# into each node's containerd. That's why manifests use imagePullPolicy: Never.
build:
	docker build -t $(IMAGE) app
	kind load docker-image $(IMAGE) --name $(CLUSTER)

ns:
	$(KUBECTL) apply -f k8s/local/00-namespace.yaml

# Same create --dry-run | apply pattern as the playground: idempotent, safe to re-run.
secrets:
	@test -f .env || (echo "Missing .env, copy .env.example and fill it in" && exit 1)
	@$(LOAD_ENV); $(KUBECTL) create secret generic ica-secrets -n $(NS) \
		--from-literal=META_TOKEN="$$META_TOKEN" \
		--from-literal=IG_USER_ID="$$IG_USER_ID" \
		--from-literal=POSTGRES_PASSWORD="$$POSTGRES_PASSWORD" \
		--from-literal=DATABASE_URL="postgresql://ica:$$POSTGRES_PASSWORD@postgres:5432/ica" \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -

tone:
	$(KUBECTL) create configmap ica-tone -n $(NS) --from-file=tone.md=$(TONE) \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -
	@echo "Tone loaded from $(TONE)"

deploy:
	$(KUBECTL) apply -f k8s/local/

model:
	$(KUBECTL) -n $(NS) rollout status deploy/ollama --timeout=300s
	$(KUBECTL) -n $(NS) exec deploy/ollama -- ollama pull $(MODEL)

# After `make build`: same tag (ica:dev), so the Deployment spec hasn't
# changed and Kubernetes won't roll it. Force it.
rollout: build
	$(KUBECTL) -n $(NS) rollout restart deploy/web

## ----- Using it -----

ui:
	@echo "Review UI on http://localhost:8000. Ctrl+C to stop"
	$(KUBECTL) -n $(NS) port-forward svc/web 8000:80

# Run a CronJob now and stream its logs. Waits for the Pod first, because
# `logs -f` errors while the container is still ContainerCreating. Note: manual
# Jobs bypass the CronJob's concurrencyPolicy, so don't start a second one
# while one is running.
ingest-now worker-now:
	@job=$(subst -now,,$@)-manual-$$(date +%s); \
	$(KUBECTL) -n $(NS) create job $$job --from=cronjob/$(subst -now,,$@) && \
	$(KUBECTL) -n $(NS) wait --for=condition=Ready pod -l job-name=$$job --timeout=120s; \
	$(KUBECTL) -n $(NS) logs -f job/$$job

# Logs from the most recent Job of each kind.
logs:
	@for j in ingest worker; do \
		job=$$($(KUBECTL) -n $(NS) get jobs -o name --sort-by=.metadata.creationTimestamp | grep $$j | tail -1); \
		[ -n "$$job" ] && echo "=== $$job ===" && $(KUBECTL) -n $(NS) logs $$job --tail=40; \
	done; true

# Score the labelled set in seeds/eval.yaml (piped in, never baked into the
# image). Compare models: make eval EVAL_MODEL=qwen2.5vl:7b
eval:
	$(KUBECTL) -n $(NS) exec -i deploy/web -- env EVAL_MODEL=$(EVAL_MODEL) python -m ica.eval < seeds/eval.yaml

draft-eval:
	$(KUBECTL) -n $(NS) exec -i deploy/web -- env EVAL_MODEL=$(EVAL_MODEL) python -m ica.drafteval < seeds/eval.yaml

psql:
	$(KUBECTL) -n $(NS) exec -it statefulset/postgres -- psql -U ica ica

## ----- Meta -----

# Read-only Business Discovery probe. Usage: make probe HANDLES="a b c"
probe:
	@$(LOAD_ENV); FB_TOKEN="$$META_TOKEN" python3 scripts/bd_probe.py $(HANDLES)

# Swap the ~1h Graph API Explorer token for a ~60-day one. Prints the new token;
# paste it into .env as META_TOKEN, then `make secrets`.
long-token:
	@$(LOAD_ENV); curl -s -G "https://graph.facebook.com/oauth/access_token" \
		--data-urlencode grant_type=fb_exchange_token \
		--data-urlencode client_id="$$META_APP_ID" \
		--data-urlencode client_secret="$$META_APP_SECRET" \
		--data-urlencode fb_exchange_token="$$META_TOKEN" \
		| python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("access_token") or d)'

## ----- Argo CD -----

# Same chart as the playground, installed with Helm (Argo CD can't install itself).
argocd:
	helm repo add argo https://argoproj.github.io/argo-helm >/dev/null
	helm repo update argo >/dev/null
	helm upgrade --install argocd argo/argo-cd -n argocd --create-namespace \
		--kube-context kind-$(CLUSTER) -f helm-values/argocd/values.yaml
	$(KUBECTL) -n argocd rollout status deploy/argocd-server --timeout=300s

# Only needed if the repo is private: Argo CD needs a read-only credential. Use a fine-grained GitHub
# token scoped to this one repo with Contents: read-only. Declarative: a Secret
# labelled secret-type=repository is how Argo CD discovers repo credentials.
argocd-repo:
	@$(LOAD_ENV); test -n "$$GITHUB_TOKEN" || (echo "Set GITHUB_TOKEN in .env" && exit 1); \
	$(KUBECTL) -n argocd create secret generic repo-ica \
		--from-literal=type=git \
		--from-literal=url=https://github.com/adamsebesta/ig-comment-assistant.git \
		--from-literal=username=adamsebesta \
		--from-literal=password="$$GITHUB_TOKEN" \
		--dry-run=client -o yaml | $(KUBECTL) label -f - --local -o yaml \
		argocd.argoproj.io/secret-type=repository | $(KUBECTL) apply -f -

argocd-app:
	$(KUBECTL) apply -f k8s/argocd/ica.yaml

argocd-password:
	@$(KUBECTL) -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo

argocd-ui:
	@echo "Argo CD on http://localhost:8081 (user: admin, password: make argocd-password). Ctrl+C to stop"
	$(KUBECTL) -n argocd port-forward svc/argocd-server 8081:80
