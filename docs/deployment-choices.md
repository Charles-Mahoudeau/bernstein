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
| Automatic rollback on failed deploy                    | Must be scripted                             | Must be scripted           | `helm upgrade --atomic`  |
| Clean removal of everything                            | Delete files one by one                      | `kubectl delete -k`        | `helm uninstall`         |
| Hooks and tests                                        | No                                           | No                         | Yes (`helm test`, hooks) |
| Installing third-party software (Traefik)              | Apply someone else's YAML                    | Same                       | One chart install        |
| Readability                                            | Plain YAML                                   | Plain YAML plus patches    | Go templates             |

Plain `kubectl` and Kustomize cover the basic deployment, but both leave release tracking and failed-deploy rollback to custom scripts. The project grades zero-downtime updates and operability, and Helm provides them directly: `helm upgrade --atomic` rolls back by itself when the new pods never become ready. Helm is also needed anyway to install the ingress controller, so using it for the app keeps a single tool.

The cost is templating that is harder to read and release state stored in the cluster. For five components this is manageable.

## Pipeline overview

On push, a GitHub Actions workflow:

1. Logs in to Azure (`azure/login`, service principal or OIDC).
2. Builds the `poll`, `worker` and `result` images and pushes them to ACR (`az acr build` or `docker/build-push-action`).
3. Tags every image with the commit SHA (never `latest`).
4. Runs `helm upgrade --install` with the new tag.

AKS pulls from ACR without an image pull secret once they are linked: `az aks update --attach-acr`.

The evaluator deploys with their own credentials and cluster, so the pipeline must not depend on our GitHub secrets. A local `scripts/deploy.py` (build, push, helm upgrade) is the primary path, and the GitHub workflow is a thin wrapper around it.

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
helm upgrade --install poll-app ./charts/poll-app --set image.tag=$SHA --atomic
helm rollback poll-app
helm uninstall poll-app
```

Traefik is a second chart, installed from its official repository. ingress-nginx was not used because it was retired in March 2026 (no more security fixes); Gateway API was considered but adds new resource types for little gain here.

## Zero-downtime updates and rollback

- Deployments use `RollingUpdate` with `maxUnavailable: 0`.
- Readiness probes make sure a new pod only receives traffic once it is ready.
- If the new pods never become ready, the rollout stalls and the old pods keep serving.
- Kubernetes alone does not roll back; it only marks the rollout failed after `progressDeadlineSeconds`. `helm upgrade --atomic --timeout 2m` waits for readiness and reverts to the previous release on failure.

## Required `.env` variables

Copy `.env.example` to `.env` (git-ignored) and fill it in. The scripts read it at startup. Neither the Azure credentials nor `POSTGRES_PASSWORD` are committed.

| Variable                 | Required                   | Purpose                                                                |
|--------------------------|----------------------------|------------------------------------------------------------------------|
| `AZURE_SUBSCRIPTION_ID`  | Yes                        | Subscription that holds the cluster and registry                       |
| `AZURE_TENANT_ID`        | Yes                        | Azure AD tenant used to log in                                         |
| `AZURE_CLIENT_ID`        | CI only                    | Service principal / OIDC app ID (otherwise `az login` is used locally) |
| `AZURE_CLIENT_SECRET`    | CI only, if not using OIDC | Service principal secret                                               |
| `AZURE_RESOURCE_GROUP`   | Yes                        | Resource group of the AKS cluster and ACR                              |
| `AZURE_AKS_CLUSTER_NAME` | Yes                        | Cluster to deploy to (`az aks get-credentials`)                        |
| `AZURE_ACR_NAME`         | Yes                        | Private registry the images are pushed to (`<name>.azurecr.io`)        |
| `POSTGRES_PASSWORD`      | Yes                        | Stored in a Kubernetes Secret created at deploy time                   |
| `AZURE_LOCATION`         | Bootstrap only             | Region for `bootstrap_azure.py` (e.g. `westeurope`)                    |
| `AZURE_AKS_NODE_COUNT`   | Bootstrap only             | Number of nodes, kept small to save credits                            |
| `NAMESPACE`              | Optional                   | Target namespace, defaults to `poll`                                   |
| `IMAGE_TAG`              | Optional                   | Defaults to the current git commit SHA                                 |

In GitHub Actions, the same values are set as repository secrets instead of a `.env` file.

## Scripts

- `scripts/bootstrap_azure.py`: resource group, ACR, AKS, attach-acr (optional, the evaluator has a cluster).
- `scripts/deploy.py`: build, push, `helm upgrade --install`.
- `scripts/update.py`: roll out a new version.
- `scripts/probe.py`: curl Poll and Result in a loop and count non-200 responses, to prove zero downtime.
- `scripts/chaos_drain.py` (optional): drain a node or delete random pods while `probe.py` runs.
- `scripts/teardown.py`: delete the resource group to save credits.

## Chaos testing

Chaos Mesh, LitmusChaos, `kube-monkey` and Azure Chaos Studio exist for automated chaos testing. They were not adopted: each adds a component to install and maintain, which is disproportionate for a five-component application.

Instead, `scripts/chaos_drain.py` drains a node or deletes random pods while `scripts/probe.py` keeps requesting Poll and Result. The run passes if no request fails, which shows that replicas, readiness probes and PodDisruptionBudgets keep the application available.

A dedicated chaos tool remains a possible future improvement.
