# Bernstein

## Deploy to Azure

Everything goes through the `scripts/bernstein` CLI. It needs [uv](https://docs.astral.sh/uv/), `az`, `kubectl`, `helm` and (for local builds) `docker`. Full guide: [docs/scripts.md](docs/scripts.md).

```shell
cp .env.example .env                   # set POSTGRES_PASSWORD at least
az login

scripts/bernstein preflight            # VM size, vCPU quota, registry name
scripts/bernstein up                   # registry, cluster, Traefik, images, deploy, vote check
scripts/bernstein status               # URLs, pods, revision

scripts/bernstein release              # rolling update to the current commit, availability probed
scripts/bernstein rollback             # back to the previous revision
scripts/bernstein chaos drain          # drain a node while probing (also: pod, node-kill, bad-release)
scripts/bernstein demo                 # the whole evaluation scenario, one report

scripts/bernstein cluster stop         # stop paying for the VMs
scripts/bernstein down --all           # delete the cluster and the registry
```

Every command prints the `az`, `kubectl`, `helm` and `docker` commands it runs, and `--dry-run` prints them without running anything.

Design notes: [docs/deployment-choices.md](docs/deployment-choices.md).
