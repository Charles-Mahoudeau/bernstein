# Bernstein

## Getting started

From a fresh clone to a running app, copy and paste in order:

```shell
git clone <repository-url> bernstein && cd bernstein
cp .env.example .env
$EDITOR .env                           # set POSTGRES_PASSWORD, POSTGRES_USER and POSTGRES_DB
az login
scripts/bernstein preflight            # check tools, quota and names before creating anything
scripts/bernstein up                   # about 4 minutes, prints the Poll and Result URLs
scripts/bernstein status               # pods, revision and URLs again
scripts/bernstein down --all           # when done: delete the cluster and the registry
```

Open the Poll URL, vote, and watch the tally change on the Result URL.

## Deploy to Azure

Everything goes through the `scripts/bernstein` CLI. It needs [uv](https://docs.astral.sh/uv/), `az`, `kubectl`, `helm` and (for local builds) `docker`. Full guide: [docs/scripts.md](docs/scripts.md).

```shell
cp .env.example .env                   # set POSTGRES_PASSWORD, POSTGRES_USER and POSTGRES_DB
az login

scripts/bernstein preflight            # VM size, vCPU quota, registry name
scripts/bernstein up                   # registry, cluster, Traefik, images, deploy, vote check (about 4 minutes)
scripts/bernstein status               # URLs, pods, revision

scripts/bernstein release              # rolling update to the current commit, availability probed
scripts/bernstein rollback             # back to the previous revision
scripts/bernstein chaos drain          # drain a node while probing (also: pod, node-kill, bad-release)
scripts/bernstein demo                 # the whole evaluation scenario, one report

scripts/bernstein cluster stop         # stop paying for the VMs
scripts/bernstein down --all           # delete the cluster and the registry
```

Notes on `.env`:

- `POSTGRES_PASSWORD`, `POSTGRES_USER` and `POSTGRES_DB` are required. `REDIS_PASSWORD` is optional: when empty, Redis runs without a password.
- `up` creates the resource group, registry and cluster if they are missing. To reuse existing ones, set `AZURE_RESOURCE_GROUP`, `AZURE_ACR_NAME` and `AZURE_AKS_CLUSTER_NAME`. When `AZURE_ACR_NAME` is empty, a registry name is generated and written to `.env`.
- Images are tagged with the current git commit SHA. Set `IMAGE_TAG` to use another tag, such as `latest`.
- `preflight` may warn that the VM size is restricted in some zones. This is harmless: the cluster is created without zones.
- At the end, `up` and `status` print the Poll and Result URLs (`poll.<traefik-ip>.nip.io`, `result.<traefik-ip>.nip.io`).

Every command prints the `az`, `kubectl`, `helm` and `docker` commands it runs, and `--dry-run` prints them without running anything.

Design notes: [docs/deployment-choices.md](docs/deployment-choices.md). Resource requests and limits, with a benchmark: [docs/resource-limits.md](docs/resource-limits.md). Autoscaling of Poll, with a load test: [docs/autoscaling.md](docs/autoscaling.md). Node count and size: [docs/node-count.md](docs/node-count.md). Traefik, its middleware and why: [docs/traefik.md](docs/traefik.md).
