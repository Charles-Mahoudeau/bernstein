# Deployment choices

Notes on the tooling decisions for the Bernstein project (Poll app on Azure Kubernetes). The deployment uses Helm only.

## Why Helm

Three ways to deploy the manifests were considered.

|                                                        | Plain `kubectl apply -f`                     | Kustomize                  | Helm                     |
|--------------------------------------------------------|----------------------------------------------|----------------------------|--------------------------|
| Extra tool to install                                  | No                                           | No (built into `kubectl`)  | Yes                      |
| Injecting the image tag per build                      | `sed` / `envsubst` hack                      | `kustomize edit set image` | `--set image.tag=$SHA`   |
| Configurable settings (replicas, resources, hostnames) | Edit the files                               | Overlays and patches       | `values.yaml`            |
| Release history                                        | No                                           | No                         | Yes (`helm history`)     |
| Rollback                                               | Script `kubectl rollout undo` per Deployment | Same script                | `helm rollback`          |
| Automatic rollback on failed deploy                    | Must be scripted                             | Must be scripted           | `helm upgrade --rollback-on-failure` |
| Clean removal of everything                            | Delete files one by one                      | `kubectl delete -k`        | `helm uninstall`         |
| Hooks and tests                                        | No                                           | No                         | Yes (`helm test`, hooks) |
| Installing third-party software (Traefik)              | Apply someone else's YAML                    | Same                       | One chart install        |
| Readability                                            | Plain YAML                                   | Plain YAML plus patches    | Go templates             |

Plain `kubectl` and Kustomize cover the basic deployment, but both leave release tracking and failed-deploy rollback to custom scripts. The project grades zero-downtime updates and operability, and Helm provides them directly: `helm upgrade --rollback-on-failure` (`--atomic` before Helm 4) rolls back by itself when the new pods never become ready. Helm is also needed anyway to install the ingress controller, so using it for the app keeps a single tool.

The cost is templating that is harder to read and release state stored in the cluster. For five components this is manageable.

## Pipeline overview

On push, a GitHub Actions workflow:

1. Logs in to Azure (`azure/login`, service principal or OIDC).
2. Builds the `poll`, `worker` and `result` images and pushes them to ACR (`az acr build` or `docker/build-push-action`).
3. Tags every image with the commit SHA (never `latest`).
4. Runs `helm upgrade --install` with the new tag.

AKS pulls from ACR without an image pull secret once they are linked: `az aks update --attach-acr`.

The evaluator deploys with their own credentials and cluster, so the pipeline must not depend on our GitHub secrets. The local `scripts/bernstein` CLI (build, push, helm upgrade, see [scripts.md](scripts.md)) is the primary path, and the GitHub workflow is a thin wrapper around it.

## The app chart

A Helm chart is a package of Kubernetes manifests. The app chart (`charts/poll-app/`) contains everything for the Poll application so one command deploys it:

- `templates/poll.yaml`: Deployment and Service for Poll
- `templates/worker.yaml`: Deployment for the Worker (no Service)
- `templates/result.yaml`: Deployment and Service for Result
- `templates/redis.yaml`: Deployment and Service for Redis
- `templates/postgres.yaml`: StatefulSet, Service and PersistentVolumeClaim
- `templates/ingress.yaml`: routes to Poll and Result
- `templates/secret.yaml`, `pdb.yaml`, `networkpolicy.yaml`, ...
- `values.yaml`: image tags, replicas, resource limits, hostnames

```sh
helm upgrade --install poll-app ./charts/poll-app --set poll.image.tag=$SHA --rollback-on-failure --wait
helm rollback poll-app
helm uninstall poll-app
```

Traefik is a second chart, installed from its official repository. ingress-nginx was not used because it was retired in March 2026 (no more security fixes); Gateway API was considered but adds new resource types for little gain here.

## Zero-downtime updates and rollback

- Deployments use `RollingUpdate` with `maxUnavailable: 0`.
- Readiness probes make sure a new pod only receives traffic once it is ready.
- If the new pods never become ready, the rollout stalls and the old pods keep serving.
- Kubernetes alone does not roll back; it only marks the rollout failed after `progressDeadlineSeconds`. `helm upgrade --rollback-on-failure --timeout 3m` waits for readiness and reverts to the previous release on failure. Helm 4 renamed `--atomic` to `--rollback-on-failure` (and `--wait` defaults to the `watcher` strategy with it); the CLI picks the right flags for Helm 3 or 4.

## Resource requests and limits

Every component sets CPU and memory requests and a memory limit, with no CPU limit except on Postgres, which is Guaranteed QoS so it is evicted last. A LimitRange covers containers that declare nothing, and there is no ResourceQuota. The reasoning, the values and the benchmark behind them are in [resource-limits.md](resource-limits.md).

## Replica counts

Poll, Result and Worker run 2 replicas, the smallest count that survives losing a pod or a node. Redis and Postgres run 1, since a second replica would not share their data. Poll (2 to 4) and Result (2 to 3) also scale on CPU. The reasoning and the benchmark figures behind it are in [replica-counts.md](replica-counts.md) and [autoscaling.md](autoscaling.md).

## Restart policies

Every workload is long-running (no Job or CronJob), so all five use `restartPolicy: Always`, the only value a Deployment or StatefulSet accepts. Liveness probes only check that the process is alive (`/healthz`), while readiness probes check the dependencies, so a Redis or PostgreSQL outage takes pods out of the Service instead of restarting them. The reference on the available policies, CrashLoopBackOff and the probe review (postgres startup probe, SIGTERM handling in `result`) is in [restart-policies.md](restart-policies.md).

## Node count and size

The cluster is 2 × `Standard_B2s_v2`. One node would hold the 0.75 CPU / 1.25 GiB of requests, but two are needed to spread the replicas and survive a drain or a node loss. The derivation is in [node-count.md](node-count.md).

## Load balancing

A request to `http://poll.<ip>.nip.io` goes through three levels, each one spreading the traffic over the level below:

```
Client
  │
  ▼
Azure Load Balancer (Standard SKU, public IP)   L4: picks a node
  │  port 80 → NodePort of the traefik Service
  ▼
AKS node (Cilium eBPF, no kube-proxy)           forwards to a Traefik pod, on this node or another
  │
  ▼
Traefik pod                                     L7: matches the Ingress host, picks an app pod
  │  pod IP taken from the EndpointSlices
  ▼
poll / result pod (Ready pods only)
```

1. **Azure Load Balancer.** Nothing in the repository creates it. The Traefik chart creates a `Service` of type `LoadBalancer`, and the AKS cloud controller adds a public IP and rules for ports 80 and 443 to the cluster load balancer (`kubernetes`, in the node resource group `MC_<resource-group>_<cluster>_<location>`). It works at layer 4: each connection goes to one node, chosen by a hash of its source and destination addresses and ports, among the nodes that pass its health probe.
2. **Traefik Service.** With `externalTrafficPolicy: Cluster`, every node accepts this traffic and forwards it to a Traefik pod, even when none runs on that node.
3. **Traefik.** Traefik reads the Ingress resources (`ingressClassName: traefik`), matches the host and sends the request straight to a pod IP from the EndpointSlices of the `poll` or `result` Service; the ClusterIP itself is not used. Pods are picked in round-robin, and only Ready pods are listed, so the readiness probes decide which pods receive traffic.

The Poll page shows `Processed by container ID <pod name>`: with several replicas, reloading the page shows the round-robin.

```sh
kubectl -n traefik get service traefik -o wide   # type, external IP, node ports
kubectl -n traefik get pods -o wide              # one Traefik pod per node
kubectl -n poll get endpointslices               # pods Traefik can send to
az network lb rule list  -g MC_<rg>_<cluster>_<location> --lb-name kubernetes -o table
az network lb probe list -g MC_<rg>_<cluster>_<location> --lb-name kubernetes -o table
```

### Choices

The Traefik settings live in `charts/traefik-values.yaml`, which `bernstein traefik install` passes to the chart.

| Setting                 | Chart default | Chosen                                          | Why                                                                                 |
|-------------------------|---------------|-------------------------------------------------|-------------------------------------------------------------------------------------|
| Traefik replicas        | 1             | 2                                               | With one pod, draining or losing its node cuts Poll and Result until it restarts    |
| Spread over the nodes   | None          | `topologySpreadConstraints`, `ScheduleAnyway`   | Two replicas on the same node fail together                                         |
| PodDisruptionBudget     | Disabled      | `minAvailable: 1`                               | A drain never evicts the last running Traefik                                       |
| `externalTrafficPolicy` | `Cluster`     | `Cluster`, set explicitly                       | See below                                                                           |
| Load balancer SKU       | Standard      | Standard                                        | AKS default, Basic is retired                                                       |
| Public IP               | Dynamic       | Dynamic                                         | See below                                                                           |
| Session affinity        | None          | None                                            | See below                                                                           |

`ScheduleAnyway` rather than `DoNotSchedule`: during a drain, the pod evicted from the drained node can start on the remaining node, which already runs a Traefik, instead of staying Pending.

**`Cluster` or `Local`.** `Local` only sends traffic to the nodes that run a Traefik pod and keeps the client IP (no extra hop, no SNAT). But the Azure health probe takes a few seconds to notice that a node lost its Traefik pod, and the requests sent to it meanwhile fail, which breaks the zero-downtime requirement during a drain. The application does not use the client IP, so `Cluster` was kept.

**Public IP.** The IP belongs to the Traefik Service: `bernstein traefik uninstall` releases it and the `nip.io` hostnames change. A static IP (`az network public-ip create` in the node resource group, then the `service.beta.kubernetes.io/azure-pip-name` annotation on the Service) would survive a reinstall. It was not set up because the CLI computes the hostnames from the current IP, and `bernstein status` prints them.

**Session affinity.** Poll keeps no server-side state (the `voter_id` cookie is stored by the browser), so any replica can answer. Result is different: its page uses Socket.IO with the `polling` transport, and a Socket.IO session lives in the memory of the pod that opened it. With several Result replicas, a request reaching another pod is rejected (`Session ID unknown`) and the client reconnects. Result runs a single replica, so this does not happen today. Before scaling it, enable Traefik sticky sessions with an annotation on the Result Service:

```yaml
traefik.ingress.kubernetes.io/service.sticky.cookie: "true"
```

## Required `.env` variables

Copy `.env.example` to `.env` (git-ignored) and fill it in. The CLI reads it at startup. Neither the Azure credentials nor `POSTGRES_PASSWORD` are committed. Every variable has a default except the secrets; the full table is in [scripts.md](scripts.md#configuration).

| Variable                 | Required                   | Purpose                                                                |
|--------------------------|----------------------------|------------------------------------------------------------------------|
| `AZURE_SUBSCRIPTION_ID`  | Recommended                | Subscription that holds the cluster and registry                       |
| `AZURE_TENANT_ID`        | Recommended                | Azure AD tenant used to log in                                         |
| `AZURE_CLIENT_ID`        | CI only                    | Service principal / OIDC app ID (otherwise `az login` is used locally) |
| `AZURE_CLIENT_SECRET`    | CI only, if not using OIDC | Service principal secret                                               |
| `AZURE_RESOURCE_GROUP`   | Default `Bernstein`        | Resource group of the AKS cluster and ACR                              |
| `AZURE_AKS_CLUSTER_NAME` | Default `bernstein-aks`    | Cluster to deploy to (`az aks get-credentials`)                        |
| `AZURE_ACR_NAME`         | Generated if empty         | Private registry the images are pushed to (`<name>.azurecr.io`)        |
| `POSTGRES_PASSWORD`      | Yes                        | Stored in a Kubernetes Secret created at deploy time                   |
| `REDIS_PASSWORD`         | Optional                   | Enables Redis authentication                                           |
| `AZURE_LOCATION`, `AZURE_AKS_*`, `AZURE_ACR_SKU` | Cluster creation only | Region, node count and size, max surge, network policy  |
| `NAMESPACE`              | Optional                   | Target namespace, defaults to `poll`                                   |
| `IMAGE_TAG`              | Optional                   | Defaults to the current git commit SHA                                 |
| `POLL_HOST`, `RESULT_HOST` | Optional                 | Ingress hostnames, default `<component>.<traefik-ip>.nip.io`           |
| `PROBE_RATE`, `PROBE_TIMEOUT` | Optional              | Availability probe settings                                            |

In GitHub Actions, the same values are set as repository secrets instead of a `.env` file.

## Scripts

A single typed Python CLI, `scripts/bernstein` (Typer, Rich, httpx; run with uv), instead of one script per task: the tasks share settings, output and the availability monitor, and one `--help` lists everything. It shells out to `az`, `kubectl`, `helm` and `docker` and prints each command, so every step can be rerun by hand. Usage: [scripts.md](scripts.md).

- `preflight`, `registry create|delete`, `cluster create|start|stop|delete|status|grant`, `up`, `down`: Azure resources. The resource group is never deleted.
- `images build [--remote]`: build and push, tagged with the commit SHA.
- `deploy`, `release`, `rollback`, `history`, `status`: Helm release; `release` is a rolling update under the availability probe.
- `probe`, `vote-check`: availability monitor and end-to-end vote check.
- `chaos pod|drain|node-kill|bad-release`, `demo`: disruption tests under the probe.

## Chaos testing

Chaos Mesh, LitmusChaos, `kube-monkey` and Azure Chaos Studio exist for automated chaos testing. They were not adopted: each adds a component to install and maintain, which is disproportionate for a five-component application.

Instead, `bernstein chaos` deletes pods, drains a node, powers off a node's VM or deploys a broken release while the availability monitor keeps requesting Poll and Result through the ingress. The run passes if no request fails, which shows that replicas, readiness probes and PodDisruptionBudgets keep the application available.

A dedicated chaos tool remains a possible future improvement.
