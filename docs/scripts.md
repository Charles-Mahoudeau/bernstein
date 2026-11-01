# The `bernstein` CLI

One command-line tool to manage the Azure resources, build and push the images, deploy and update the Helm release, and prove that the application stays available during updates and failures.

```sh
scripts/bernstein up                 # registry + cluster + Traefik + images + app, from nothing
scripts/bernstein release            # rolling update to the current commit, under the availability probe
scripts/bernstein chaos drain        # drain a node while probing
scripts/bernstein demo               # the whole evaluation scenario, one report
scripts/bernstein down --all         # delete the cluster and the registry
```

- [Install](#install)
- [Configuration](#configuration)
- [How the output reads](#how-the-output-reads)
- [Quick start](#quick-start)
- [Commands](#commands)
- [The availability monitor](#the-availability-monitor)
- [Disruption tests](#disruption-tests)
- [What the chart needs for 100%](#what-the-chart-needs-for-100)
- [Cost control](#cost-control)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Install

Required tools:

| Tool                                          | Used for                                                    |
|-----------------------------------------------|-------------------------------------------------------------|
| [uv](https://docs.astral.sh/uv/)              | Runs the CLI with its Python dependencies (Python ≥ 3.12)   |
| `az` (Azure CLI)                              | Registry, cluster, VM operations                            |
| `kubectl`                                     | Namespace, Secrets, pods, nodes                             |
| `helm` (3 or 4)                               | Traefik and the app release                                 |
| `docker` with `buildx`                        | Local image builds (optional with `images build --remote`)  |

There is nothing else to install. The first run creates `scripts/.venv` and downloads the dependencies (Typer, Rich, httpx, python-dotenv, python-socketio).

Three ways to run it, all equivalent:

```sh
scripts/bernstein --help                       # wrapper, works from any directory and any shell
uv run --project scripts bernstein --help      # what the wrapper does
uv tool install --editable ./scripts           # puts `bernstein` on PATH (needed for completion)
bernstein --install-completion                 # bash, zsh or fish completion, after the line above
```

Log in to Azure once with `az login` (or `az login --tenant <tenant>`). In CI, set `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` and `AZURE_TENANT_ID` and the CLI logs in with the service principal.

## Configuration

Copy `.env.example` to `.env` (git-ignored) and fill it in. Precedence, highest first: command-line flags, environment variables, `.env`, defaults. Empty values count as unset. `--env-file PATH` reads another file.

| Variable                   | Default                                  | Purpose                                                                                    |
|----------------------------|------------------------------------------|--------------------------------------------------------------------------------------------|
| `AZURE_SUBSCRIPTION_ID`    | `az` current subscription                | Passed as `--subscription` to every `az` command                                           |
| `AZURE_TENANT_ID`          |                                          | Service principal login, `az login` hint                                                   |
| `AZURE_CLIENT_ID`          |                                          | Service principal login (CI only)                                                          |
| `AZURE_CLIENT_SECRET`      |                                          | Service principal login (CI only)                                                          |
| `AZURE_RESOURCE_GROUP`     | `Bernstein`                              | Holds the cluster and registry. Created if missing, **never deleted**                      |
| `AZURE_AKS_CLUSTER_NAME`   | `bernstein-aks`                          | Cluster name                                                                               |
| `AZURE_ACR_NAME`           | generated                                | Registry name. `registry create` generates `bernsteinacr<hex>` and writes it to `.env`     |
| `AZURE_LOCATION`           | `spaincentral`                           | Region of new resources                                                                    |
| `AZURE_AKS_NODE_COUNT`     | `2`                                      | Nodes of the node pool                                                                     |
| `AZURE_AKS_VM_SIZE`        | `Standard_B2s_v2`                        | Node size                                                                                  |
| `AZURE_AKS_OS_DISK_GB`     | `64`                                     | Node OS disk                                                                               |
| `AZURE_AKS_MAX_SURGE`      | `1`                                      | Extra nodes during node upgrades (`1`, `33%`, ...)                                         |
| `AZURE_AKS_NODEPOOL`       | `system`                                 | Node pool name                                                                             |
| `AZURE_AKS_NETWORK_POLICY` | `cilium`                                 | `cilium`, `azure`, `calico` or `none`, needed for NetworkPolicies. Creation only           |
| `AZURE_ACR_SKU`            | `Basic`                                  | Registry SKU                                                                               |
| `NAMESPACE`                | `poll`                                   | Namespace of the app                                                                       |
| `HELM_RELEASE`             | `poll-app`                               | Release name                                                                               |
| `KUBE_CONTEXT`             | cluster name                             | kubectl/helm context. Every command passes it, so another current context is never touched |
| `TRAEFIK_NAMESPACE`        | `traefik`                                | Namespace of the Traefik release                                                           |
| `POLL_HOST`, `RESULT_HOST` | `poll.<ip>.nip.io`, `result.<ip>.nip.io` | Ingress hostnames. nip.io resolves `<anything>.<ip>.nip.io` to `<ip>`, no DNS needed       |
| `IMAGE_TAG`                | git short SHA                            | Image tag (see [images build](#images))                                                    |
| `PROBE_RATE`               | `5`                                      | Requests per second per probed URL                                                         |
| `PROBE_TIMEOUT`            | `2`                                      | Seconds before a probe request counts as failed                                            |
| `POSTGRES_PASSWORD`        | required for `deploy`                    | Written to the Secret `postgres-secret`                                                    |
| `POSTGRES_USER`            | required for `deploy`                    | PostgreSQL user, written to `postgres-secret` (not masked in the output)                   |
| `POSTGRES_DB`              | required for `deploy`                    | PostgreSQL database, written to `postgres-secret` (not masked in the output)               |
| `REDIS_PASSWORD`           | none                                     | Set: Secret `redis-secret` and `redis.auth.enabled=true`. Empty: Redis without password    |

The defaults come from the setup that works on the "Azure for Students" subscription (France Central has no usable small VM size, Spain Central has `Standard_B2s_v2` with enough quota).

## How the output reads

Every external command is printed before it runs, ready to copy and paste:

```text
==> Creating AKS cluster bernstein-aks (about 5-10 min)
$ az aks show -g Bernstein -n bernstein-aks -o json --subscription 221c...      <- dimmed: read-only query
$ az aks create -g Bernstein -n bernstein-aks -l spaincentral --node-count 2 ...  <- highlighted: changes something
  │ <output of the command, streamed and indented>
    took 6m12s
OK  cluster bernstein-aks is ready
```

| Marker                        | Meaning                                                                   |
|-------------------------------|---------------------------------------------------------------------------|
| `==>`                         | A stage of the command                                                    |
| `$`                           | A command (dimmed = read-only query, highlighted = action)                |
| `│`                           | Output of the command above                                               |
| `[12:03:04 +5.0s]`            | Progress of a wait loop or of the monitor, printed when the state changes |
| `OK`, `WARN`, `FAIL`, `ERROR` | Result lines; errors go to stderr                                         |
| `??`                          | A confirmation prompt                                                     |

Secrets (`POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `AZURE_CLIENT_SECRET`) never appear: Secrets are sent to `kubectl apply -f -` on stdin, and any secret value in a command or its output is replaced by `***`. The vote check is not a subprocess; it is echoed as the equivalent `curl` command.

Global options go before the command (`scripts/bernstein -n -q deploy`):

| Option                        | Effect                                                                                                               |
|-------------------------------|----------------------------------------------------------------------------------------------------------------------|
| `--dry-run`, `-n`             | Print the actions without running them. Queries still run, so the plan matches the real state                        |
| `--quiet`, `-q`               | Hide queries and details                                                                                             |
| `--verbose`, `-v`             | Also print the output of queries                                                                                     |
| `--color auto\|always\|never` | `auto` colors only a terminal and honours `NO_COLOR` and `FORCE_COLOR`. Without colors, the markers keep the meaning |
| `--yes`, `-y`                 | Answer yes to confirmations. Without a terminal and without `--yes`, a confirmation refuses                          |
| `--env-file PATH`             | Settings file (default `.env` at the repo root)                                                                      |

Deletions (`cluster delete`, `registry delete`, `uninstall --delete-namespace`) ask you to type the resource name.

Output is plain lines (no progress bars or live widgets), so it reads the same in a terminal, a CI log or a file.

Exit codes: `0` success, `1` error (a failed command is shown with the end of its output), `2` usage error, `3` availability below the threshold.

## Quick start

### Fresh subscription (or the evaluator's own)

```sh
cp .env.example .env            # set POSTGRES_PASSWORD, POSTGRES_USER and POSTGRES_DB at least
az login
scripts/bernstein preflight     # VM size restrictions, vCPU quota, registry name
scripts/bernstein up            # 15-20 min: registry, cluster, Traefik, images, deploy, vote check
scripts/bernstein status        # URLs, pods, revision
```

### Existing cluster (day-to-day)

```sh
scripts/bernstein cluster start                 # if it was stopped
scripts/bernstein release                       # build the current commit and roll it out under the probe
scripts/bernstein chaos pod -c poll --count 3   # break things
scripts/bernstein cluster stop                  # stop paying for VMs
```

Each team member can use their own `NAMESPACE` on the shared cluster.

## Commands

`scripts/bernstein <command> --help` lists every option.

### Azure

| Command                    | What it does                                                                                                                                                                                                                                                                          |
|----------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `preflight`                | Tools and versions, Azure login, VM size restrictions in the region, vCPU quota for nodes + surge (total and VM family), registry name availability. Suggests fallback sizes and regions on failure                                                                                   |
| `registry create`          | Registers `Microsoft.ContainerRegistry` if needed, creates the resource group if missing, generates `AZURE_ACR_NAME` into `.env` if empty, creates the ACR. Idempotent                                                                                                                |
| `registry show`            | Login server, SKU, repositories and their latest tags                                                                                                                                                                                                                                 |
| `registry login`           | `az acr login` (Docker credentials for pushes)                                                                                                                                                                                                                                        |
| `registry delete`          | Deletes the registry and every image (type the name to confirm)                                                                                                                                                                                                                       |
| `cluster create`           | Creates the cluster (`--attach-acr`, Azure CNI Overlay, network policy, free tier), then sets the node pool max surge, fetches credentials and waits for the nodes. On an existing cluster it attaches the ACR if the kubelet lacks `AcrPull`, sets max surge and fetches credentials |
| `cluster start` / `stop`   | Starts or stops (pauses) the cluster; `start` waits for the nodes                                                                                                                                                                                                                     |
| `cluster status`           | Power state, node pools, nodes, and the billed public IPs and disks of the node resource group                                                                                                                                                                                        |
| `cluster scale N`          | Changes the number of nodes                                                                                                                                                                                                                                                           |
| `cluster credentials`      | `az aks get-credentials` into `~/.kube/config` (context named after the cluster)                                                                                                                                                                                                      |
| `cluster grant EMAIL`      | Gives a teammate "Azure Kubernetes Service Cluster User Role" on the cluster and `AcrPush` on the registry                                                                                                                                                                            |
| `cluster delete`           | Deletes the cluster; the registry is kept (type the name to confirm)                                                                                                                                                                                                                  |
| `traefik install`          | Installs or upgrades Traefik from its official chart (`charts/traefik-values.yaml` if it exists) and waits for its public IP                                                                                                                                                          |
| `traefik ip` / `uninstall` | Prints the public IP / removes Traefik                                                                                                                                                                                                                                                |
| `up`                       | `preflight` → `registry create` → `cluster create` → `traefik install` → `images build` → `deploy` → `vote-check`                                                                                                                                                                     |
| `down [--all]`             | `cluster delete`, and with `--all` `registry delete`. The resource group is kept                                                                                                                                                                                                      |

### Images

| Command                          | What it does                                                                                 |
|----------------------------------|----------------------------------------------------------------------------------------------|
| `images list`                    | Components: every `poll-application/<name>/Dockerfile` (`poll`, `result`, `worker`)          |
| `images build`                   | `docker buildx build --platform linux/amd64 --push` for each component, after `az acr login` |
| `images build --remote`          | `az acr build` instead: built in Azure, no local Docker                                      |
| `images build -c poll --no-push` | Only `poll`, loaded into the local Docker                                                    |

The tag is `--tag`, else `IMAGE_TAG`, else the short commit SHA. Uncommitted changes under `poll-application/` add `-dirty-<timestamp>`, so a different build never reuses a tag. A tag already in the registry is skipped (`--force` rebuilds). Images are never tagged `latest`: each Helm revision points to exact images, so a rollback restores exact images.

### Release

| Command                          | What it does                                                                                                                                                                                                                                                    |
|----------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `deploy [--tag T] [--set k=v]`   | Creates the namespace, applies the Secrets, installs Traefik if absent, then `helm upgrade --install` with the registry, the image tags, the ingress hosts and `--rollback-on-failure --wait=watcher --timeout 3m` (Helm 3: `--atomic --wait`). Prints the URLs |
| `release`                        | `images build`, then `deploy` under the [availability monitor](#the-availability-monitor), then `vote-check`                                                                                                                                                    |
| `release --same-images`          | Copies the deployed images to a new tag inside the registry (`az acr import`, no rebuild) and rolls it out: a real rolling update of every Deployment with the same code                                                                                        |
| `rollback [REVISION]`            | `helm rollback` to the previous (or given) revision under the monitor                                                                                                                                                                                           |
| `history`                        | Revisions of the release                                                                                                                                                                                                                                        |
| `status`                         | Revision, workloads (ready/desired, image), pods (node, ready, restarts), URLs                                                                                                                                                                                  |
| `uninstall [--delete-namespace]` | `helm uninstall`; optionally the namespace, its Secrets and volumes                                                                                                                                                                                             |

`--set` values are passed last, so they override the CLI's own (`--set poll.replicas=3`).

What happens when a release is bad: the new pods never become ready, the old pods keep serving (rolling update with readiness probes), Helm waits until the timeout, marks the upgrade failed and rolls back to the previous revision by itself. `chaos bad-release` tests exactly this.

### Checks

| Command        | What it does                                                                                                                                                                                    |
|----------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `probe [-d S]` | Runs the monitor alone for `S` seconds or until Ctrl-C, e.g. in a second terminal during a manual operation                                                                                     |
| `vote-check`   | Connects to Result over Socket.IO, reads the tally, casts a vote on Poll with a fresh `voter_id` and waits until the tally includes it. This covers poll → Redis → worker → PostgreSQL → result |

## The availability monitor

`release`, `rollback`, `probe` and every `chaos` test run inside the monitor:

1. One thread per URL sends `GET` requests through the public Traefik IP at `PROBE_RATE` per second, like a user would: `poll /`, `result /` and the Socket.IO handshake `result /socket.io/?EIO=4&transport=polling` (which reaches the Node process, not only static files). Any status ≥ 400, timeout (`PROBE_TIMEOUT`) or connection error is a failure.
2. One thread samples the ready/desired replicas of every Deployment and StatefulSet of the release every 2 seconds.
3. 2 seconds of baseline before the operation (with a warning if the app already fails), 2 seconds of tail after it.

While it runs:

```text
[14:02:11 +2.0s] event: operation starts
[14:02:15 +5.0s] ok poll 25/25 · result 25/25 · socket.io 25/25 | ready poll 2/2 redis 1/1 result 2/2 worker 2/2
[14:02:31 +21.4s] FAIL result: HTTP 502
[14:02:32 +22.6s] recovered result after 6 failure(s) in 1.2s
```

At the end: a table per URL (requests, failures, availability, p50/p95/max latency, error kinds), each failure window, the minimum ready replicas per component and the time spent at 0 ready, then `PASS` or `FAIL`.

The run fails (exit code 3) if **any** URL is below `--min-availability`. The default is **100** (one failed request fails the run) for `probe`, `release`, `rollback` and `demo`, and **99** for every `chaos` command: a node loss has a window the chart cannot remove (about 17 s of load balancer routing to the dead node), and a drain of the node that holds Postgres takes it down for 20 to 30 s ([node-failure.md](node-failure.md)). Pass `--min-availability 100` to require a perfect chaos run, or lower it further, e.g. `chaos node-kill --min-availability 95`.

Reports are written to `reports/` (git-ignored):

- `reports/<date>-<name>.json`: settings, events with timestamps, per-URL statistics and failure windows, replica timeline, verdict;
- `reports/<date>-<name>.csv`: every request (`t, target, status, latency_ms, error`), to plot or quote in the project report.

## Disruption tests

All of them run under the monitor and restore the cluster in a `finally` block: an error or Ctrl-C still uncordons the node or starts the VM again.

| Command                                             | Failure simulated                         | What happens                                                                                                                                                                                                                               |
|-----------------------------------------------------|-------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `chaos pod`                                         | A pod crashes                             | Deletes one random pod of the release, waits until every workload is ready again                                                                                                                                                           |
| `chaos pod -c poll --count 5 --interval 10`         | Repeated crashes of one component         | 5 rounds, 10 s apart                                                                                                                                                                                                                       |
| `chaos pod -c poll --all`                           | Every replica of a component dies at once | Expected to fail: the one case spare replicas cannot cover                                                                                                                                                                                 |
| `chaos drain [--node N]`                            | Node maintenance (graceful)               | `kubectl drain` (respects PodDisruptionBudgets) the node with the most app pods, waits until every workload has one ready replica (the others stay Pending while the node is cordoned), `kubectl uncordon`.                                                                                                                     |
| `chaos node-kill [--node N] [--down-time 120]`      | Node crash (hard)                         | Finds the node's VMSS instance from its `providerID`, `az vmss deallocate` (sudden power-off), waits for `NotReady`, keeps it down, `az vmss start`, waits for the node and the app                                                        |
| `chaos bad-release [--mode probe\|image] [-c poll]` | A broken version is deployed              | `helm upgrade --reuse-values` with one broken value: `probe` points the readiness probe to a 404 path, `image` uses a tag that does not exist. Expects Helm to fail and roll back, checks the release is back on the previous tag          |
| `demo [--with-node-kill]`                           | All of the above                          | vote check → `release --same-images` → `rollback` → `bad-release` → `chaos pod --count 3` → `chaos drain` → (`node-kill`) → vote check. Asks once, continues after a failed step, prints one summary and writes `reports/<date>-demo.json` |

Notes:

- After a node kill, Kubernetes marks the node `NotReady` after about 40-50 s and only evicts its pods after 5 minutes. Requests sent to that node's pods in the first seconds can fail; `--down-time 360` shows the pods being rescheduled.
- After a drain, pods are not moved back to the uncordoned node; they spread again on the next rollout.
- A node kill on a 1-node cluster, or a drain without a spare node, is a full outage; the CLI warns first.

## What the chart needs for 100%

The CLI measures availability; the chart provides it. With 1 replica per component the tests correctly report failures. Zero downtime needs, per component:

- `replicas` ≥ 2 and pods spread over nodes (`topologySpreadConstraints` or anti-affinity), so losing a pod or a node leaves one serving;
- a readiness probe, so traffic only reaches ready pods;
- `strategy.rollingUpdate.maxUnavailable: 0`, so a rollout adds a new pod before removing an old one;
- a PodDisruptionBudget (`minAvailable: 1`), so `kubectl drain` does not evict every replica at once;
- for Result with several replicas: Socket.IO's polling transport needs sticky sessions (or the websocket transport), otherwise `vote-check` and the Socket.IO probe fail.

Redis has one replica by design (`Recreate` strategy): deleting its pod makes Poll's readiness fail for a few seconds, which `chaos pod -c redis` shows.

## Cost control

About EUR 60-70 per month while running, almost all of it the nodes.

| Command                | What still costs                                                                 |
|------------------------|----------------------------------------------------------------------------------|
| `cluster stop`         | Registry (~EUR 4.5/month), disks, public IPs (~EUR 3/month each). No VM cost     |
| `down`                 | Registry only (images kept)                                                      |
| `down --all`           | Nothing created by the CLI (the resource group is kept, empty)                   |

`cluster status` lists the billed public IPs and disks. A stopped cluster keeps its state and public IPs for up to 12 months; tell your teammate before stopping it.

## Troubleshooting

| Problem                                             | Fix                                                                                                                                              |
|-----------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| `preflight`: size restricted in the region          | Set `AZURE_AKS_VM_SIZE` to `Standard_B2as_v2` or `Standard_B2ls_v2`, or `AZURE_LOCATION` to `denmarkeast` / `austriaeast`                        |
| `preflight`: not enough vCPU quota                  | Lower `AZURE_AKS_NODE_COUNT` or `AZURE_AKS_MAX_SURGE`, or request more quota in the portal                                                       |
| `the Azure CLI is not logged in`                    | `az login --tenant <tenant>`                                                                                                                     |
| Pods in `ImagePullBackOff`                          | `cluster create` again: it re-attaches the registry if the kubelet lacks `AcrPull`. Check the tag with `registry show`                           |
| `the cluster is stopped`                            | `cluster start`                                                                                                                                  |
| `context "bernstein-aks" does not exist`            | `cluster credentials`, or set `KUBE_CONTEXT`                                                                                                     |
| `deploy` times out and rolls back                   | `status` and `kubectl -n <ns> describe pod ...`; the previous revision is serving                                                                |
| Drain hangs                                         | A PodDisruptionBudget that can never be satisfied (e.g. `minAvailable: 1` with 1 replica); it stops after `--timeout` and the node is uncordoned |
| Probe fails before the operation starts             | The app is already broken: `status`, `vote-check`                                                                                                |
| Node pool count differs from `AZURE_AKS_NODE_COUNT` | `cluster scale <n>`                                                                                                                              |
| Network policy on an existing cluster               | `AZURE_AKS_NETWORK_POLICY` only applies at creation; see `az aks update --network-policy`                                                        |

## Development

Code in `scripts/src/bernstein/`, one module per area:

| Module          | Content                                                                 |
|-----------------|-------------------------------------------------------------------------|
| `cli.py`        | Typer command tree and global options                                   |
| `console.py`    | Output, colors and fallback, confirmations                              |
| `runner.py`     | Running, echoing and masking commands, dry run                          |
| `config.py`     | Settings from `.env` and the environment                                |
| `azure.py`      | Pre-flight, registry, cluster                                           |
| `kube.py`       | Typed views of workloads, pods, nodes; wait loops                       |
| `images.py`     | Build, push, retag                                                      |
| `helm.py`       | Traefik, Secrets, deploy, history, rollback                             |
| `probe.py`      | Availability monitor and reports                                        |
| `vote.py`       | End-to-end vote check                                                   |
| `chaos.py`      | Disruption tests                                                        |
| `scenarios.py`  | `release`, `up`, `down`, `demo`                                         |

```sh
cd scripts
uv run ruff format          # format
uv run ruff check           # lint
uv run mypy                 # strict type checking
```
