# Task list

Split between two people, following `docs/deployment-choices.md`. Both people write Helm templates and Kubernetes config; the split is by slice of the app, not by layer.

- **A: data path.** Redis, PostgreSQL, Worker.
- **B: web path.** Poll, Result, ingress.
- **Scripting: unassigned.** Azure bootstrap, image build, deploy/update scripts, CI workflow, probe and chaos scripts.

Each person owns the full K8s treatment (Deployment, probes, resources, PDB, network policy, security context) for their own components, so both learn the same things.

Labels: `area:helm`, `area:azure`, `area:ci`, `area:docs`, `area:test`. Size: S (<1h), M (half day), L (day).

---

## T0. Chart skeleton and contract (A + B together, M)

Do this pair-programming style so both understand the chart layout.

**Tasks**
- [ ] `charts/poll-app/` with `Chart.yaml`, `values.yaml`, `_helpers.tpl` (names, labels, image helper)
- [ ] Agree on `values.yaml` layout: `image.registry`, `image.tag`, one block per component (`poll`, `worker`, `result`, `redis`, `postgres`) with `replicas` and `resources`, plus `ingress.*`
- [ ] Agree on names: release `poll-app`, namespace `poll`, Secret `postgres-secret` (key `POSTGRES_PASSWORD`), Service names `redis` and `postgres`
- [ ] `.env.example` from the variables in `deployment-choices.md`; add `.env` and `.idea/` to `.gitignore`
- [ ] Each person works in their own template files, so there are no merge conflicts

**Done when** the skeleton is on `main` and both branch from it.

---

# Person A: data path

## A1. Redis (M, `area:helm`)

**Tasks**
- [ ] `templates/redis.yaml`: Deployment (Redis 8.x) + ClusterIP Service
- [ ] Optional `requirepass` from a Secret, exposed to the other services as `REDIS_PASSWORD`
- [ ] Probes (`redis-cli ping`), requests/limits
- [ ] NetworkPolicy: only poll and worker may connect

## A2. PostgreSQL with persistence and schema (L, `area:helm`)

**Tasks**
- [ ] `templates/postgres.yaml`: StatefulSet (Postgres 18), Service, PVC on the AKS default storage class
- [ ] Credentials from the Secret (`POSTGRES_USER`, `POSTGRES_DB`, `POSTGRES_PASSWORD`)
- [ ] Apply `poll-application/schema.sql` (Helm hook Job or init script), idempotent
- [ ] Readiness probe (`pg_isready`), requests/limits, non-root `securityContext`
- [ ] NetworkPolicy: only worker and result may connect
- [ ] Verify data survives `kubectl delete pod postgres-0`

## A3. Worker (M, `area:helm`)

**Tasks**
- [ ] `templates/worker.yaml`: Deployment, no Service, port 80 for the health endpoints
- [ ] Env per `poll-application/README.md` (`REDIS_*`, `POSTGRES_*`)
- [ ] Liveness `/healthz`, readiness `/readyz`
- [ ] `RollingUpdate` with `maxUnavailable: 0`, replicas >= 2, requests/limits, `securityContext`
- [ ] PodDisruptionBudget, anti-affinity or `topologySpreadConstraints`

**Done when** a vote pushed in Redis ends up in the `votes` table, and killing one worker pod loses nothing.

---

# Person B: web path

## B1. Poll (M, `area:helm`)

**Tasks**
- [ ] `templates/poll.yaml`: Deployment + Service, port 80
- [ ] Env: `REDIS_HOST`, `REDIS_PASSWORD`, `OPTION_A..D` from `values.yaml`
- [ ] Liveness `/healthz`, readiness `/readyz` (503 when Redis is down: check that it is taken out of the Service)
- [ ] `RollingUpdate` with `maxUnavailable: 0`, replicas >= 2, requests/limits, `securityContext`
- [ ] PodDisruptionBudget, anti-affinity or `topologySpreadConstraints`
- [ ] Optional HPA

## B2. Result (M, `area:helm`)

**Tasks**
- [ ] `templates/result.yaml`: Deployment + Service, port 80
- [ ] Env: `POSTGRES_*`
- [ ] Probes, rolling update, replicas >= 2, requests/limits, `securityContext`, PDB, anti-affinity
- [ ] Socket.IO with several replicas: check that live updates still work through the Service (sticky sessions or websocket-only transport may be needed)

## B3. Ingress (M, `area:helm`)

**Tasks**
- [ ] Install Traefik from its official chart (ingress-nginx was retired in March 2026); keep the values in `charts/traefik-values.yaml` and document the command (S3 uses it)
- [ ] `templates/ingress.yaml` (`ingressClassName: traefik`): Poll and Result via hostnames from `values.yaml` (nip.io if no DNS)
- [ ] Websocket works through Traefik (`/socket.io`)
- [ ] NetworkPolicy: Traefik may reach poll and result; poll → redis, result → postgres egress
- [ ] Optional: TLS with cert-manager

**Done when** a vote cast in the browser on Poll shows up live on Result through one public IP.

---

# Scripting (unassigned)

Not assigned yet. Pick these up as people free up; each task is independent of who writes the Helm templates. All scripts live in `scripts/`, read `.env`, and share `scripts/common.py`.

## S1. Shared helpers, Azure bootstrap and teardown (M, `area:azure`)

**Tasks**
- [ ] `scripts/common.py`: load `.env`, validate variables, run subprocesses, resolve `IMAGE_TAG` (default = git SHA). Every other script reuses this
- [ ] `scripts/bootstrap_azure.py`: resource group, ACR, AKS with 2+ nodes, `--attach-acr`, network policy enabled
- [ ] Idempotent, clear error on missing variables
- [ ] `scripts/teardown.py`: delete the resource group (with confirmation)

## S2. Build and push images (M, `area:ci`)

**Tasks**
- [ ] `scripts/build.py` (or a function used by deploy/update): build `poll`, `worker`, `result` from `poll-application/<svc>`, tag with the commit SHA, push to ACR (`az acr build` or `docker build` + push)
- [ ] Check each image starts locally with the env vars from the README

## S3. `deploy.py` (M, `area:azure`)

Needs the chart (A1-A3, B1-B3) and S1-S2.

**Tasks**
- [ ] `az aks get-credentials`, create namespace
- [ ] Create/update `postgres-secret` from `POSTGRES_PASSWORD`
- [ ] Install Traefik if absent (command from B3)
- [ ] `helm upgrade --install poll-app ./charts/poll-app --set image.tag=$SHA --atomic --timeout 2m`
- [ ] Print the access URLs at the end

## S4. `update.py` (S, `area:azure`)

**Tasks**
- [ ] Build + push new tag + `helm upgrade --atomic`, print the rollout result
- [ ] Document `helm rollback poll-app`

## S5. GitHub Actions workflow (M, `area:ci`)

**Tasks**
- [ ] `.github/workflows/deploy.yml`: on push to `main`, `azure/login` (OIDC preferred), call `scripts/deploy.py`
- [ ] Optional PR job: `helm lint`, `helm template`, image build without deploy
- [ ] List the required repo secrets in the docs

## S6. Zero-downtime proof and chaos (L, `area:test`)

Needs a deployed app and S4.

**Tasks**
- [ ] `scripts/probe.py`: loop on Poll and Result, count non-200 and connection errors, print a summary
- [ ] Run during `update.py` (0 failures expected)
- [ ] Run during a **bad** update (image that fails readiness): old pods keep serving, `--atomic` rolls back
- [ ] `scripts/chaos_drain.py`: delete random pods or drain a node (then uncordon) while `probe.py` runs
- [ ] Keep the outputs for the report

---

# Together

## C1. Helm tests and chart lint (S, `area:helm`)
- [ ] A writes the test for A's side (vote reaches Postgres), B for B's side (`/readyz` of poll and result): `templates/tests/`
- [ ] `helm lint` clean, `NOTES.txt` with access URLs

## C2. Documentation and demo (M, `area:docs`)
- [ ] Top-level `README.md`: architecture, prerequisites, quick start (`bootstrap` → `deploy` → `update` → `teardown`)
- [ ] `.env` variables, pipeline, rollback
- [ ] Demo script: deploy, vote, update with probe, bad update with rollback, chaos run

## C3. End-to-end validation (M, `area:test`)
- [ ] Full run from a fresh clone with only `.env` filled in
- [ ] No secrets in git history
- [ ] Nothing left billing after `teardown.py`

---

## Order

```
T0 (together)
 ├─ A1 → A2 → A3 ──┐
 ├─ B1 → B2 → B3 ──┼→ S3 (deploy) → S4 (update) → S6 (probe, chaos) → C1 → C3 → C2
 └─ S1 → S2 ───────┘
      S5 (CI) after S3
```

A and B need images to test end to end. Until S2 exists, push them by hand with `az acr build`. S6 needs a deployed app, so it comes last.
