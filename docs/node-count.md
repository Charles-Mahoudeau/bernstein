# Node count and size

Why the AKS cluster is 2 × `Standard_B2s_v2` (2 vCPU, 8 GiB each), derived from the resource totals in [resource-limits.md](resource-limits.md) and the replica counts in [replica-counts.md](replica-counts.md). The defaults live in `scripts/src/bernstein/config.py` and can be overridden with `AZURE_AKS_NODE_COUNT` and `AZURE_AKS_VM_SIZE`.

## What has to fit

| Component | Replicas | CPU request | Memory request | Memory limit |
|---|---|---|---|---|
| poll | 2 | 2 × 50m | 2 × 96Mi | 2 × 192Mi |
| result | 2 | 2 × 100m | 2 × 128Mi | 2 × 256Mi |
| worker | 2 | 2 × 100m | 2 × 128Mi | 2 × 512Mi |
| redis | 1 | 50m | 64Mi | 128Mi |
| postgres | 1 | 200m | 512Mi | 512Mi |
| **Total** | 8 pods | **750m** | **1,280Mi (1.25 GiB)** | 2,432Mi |

The scheduler places pods by request, so 750m and 1.25 GiB is what the app needs to be scheduled. The memory limits add up to 2.4 GiB, which is the worst case if every pod hit its limit at once. The benchmark peaks were far below that (at most 65 MiB per pod), so it is not a realistic case.

The node also runs AKS system pods (CoreDNS, the CNI, kube-proxy, metrics-server, ...) and Traefik, and AKS reserves part of each node for the kubelet and the OS. These are not part of the totals above. A `B2s_v2` leaves roughly 1.9 vCPU and 5 to 6 GiB allocatable (AKS reserves a share that grows with node size); read the real figures with `kubectl describe node | grep -A6 Allocatable`.

## Number of nodes: 2

The count comes from availability, not capacity.

- **Capacity needs one node.** 0.75 CPU and 1.25 GiB fit on a single node with a wide margin, even after the system pods and Traefik.
- **Availability needs two.** The point of two replicas is to survive losing a node, and that only works if they are on different nodes. The `topologySpreadConstraints` in the chart spread Poll, Result and Worker across hostnames, and the PodDisruptionBudgets (`minAvailable: 1`) make a drain evict one replica at a time. With a single node there is nowhere to spread to or to drain onto. `bernstein chaos drain` and `chaos node-kill` need a second node to reschedule onto.
- **Upgrades need a spare.** The node pool uses `max surge` 1: during a node image or Kubernetes upgrade AKS adds a temporary third node, then drains and replaces the others one at a time. The PDBs hold one replica up throughout. With `max surge` 0 the same upgrade would drain a node with nowhere to move its pods.
- **Three is not needed.** A third node would not improve what the project tests, since the app survives the loss of one node. It would cost a third VM and use up the student subscription's vCPU quota faster (`bernstein preflight` checks the quota; the upgrade surge already needs 6 vCPU).

### After losing a node

If one node is drained or powered off, every pod has to fit on the other. The 750m / 1.25 GiB total is well within one node's allocatable space, so all 8 pods stay scheduled. The only pods that do not move instantly are the single-replica Redis and Postgres: Redis restarts on the other node, and Postgres follows its persistent volume (an Azure disk is zonal, so it can only move to a node in the same zone as the disk). During that restart voting or results are briefly unavailable, as described in [replica-counts.md](replica-counts.md).

## Node size: B2s_v2

- **vCPU.** The 750m total is small, but Poll is CPU-bound: one gunicorn worker used about 85% of a core in the benchmark and has no CPU limit. A node with at least 2 vCPU leaves a spare core for Poll to burst into and for the system pods. A 1 vCPU size would be crowded by the system pods and would throttle Poll under load. AKS also does not allow system node pools below 2 vCPU and 4 GiB.
- **Memory.** 8 GiB is far more than the 1.25 GiB requested, which is deliberate: the 4 GiB sizes would still fit the app, but the AKS reservation and system pods use a larger share of them, and the memory limits (2.4 GiB total) must fit with the system pods if a pod regularly bursts. 8 GiB leaves comfortable room for the one-node case above.
- **Burstable B-series.** The workload is mostly idle with short bursts, so the cheapest class is enough, and it keeps the cost low on a student budget. The trade-off is CPU credits: under sustained load above the baseline a burstable VM is throttled. The benchmark ran for 60 to 90 seconds on a desktop, so it does not show how this behaves, and the results should be re-checked on the cluster (see [resource-limits.md](resource-limits.md#re-measuring-on-the-cluster)).
- **Fallbacks.** The student subscription restricts some sizes by region, so `bernstein preflight` falls back to `Standard_B2as_v2` or `Standard_B2ls_v2` (also 2 vCPU). `B2ls_v2` has 4 GiB, which still holds the app (see above).
- **OS disk.** 64 GB is the default for the node image and container images. Postgres data lives on its own persistent volume, not on the node disk.

## What would change the numbers

| Change | Effect |
|---|---|
| Poll gets more gunicorn workers or replicas | More CPU per replica. Add vCPU (a bigger size) before adding nodes, since Poll is the one component limited by CPU. |
| Replicas of Poll, Result or Worker go above 2 | The totals grow linearly, still far below one node. A third node is only needed if the pods no longer fit on N-1 nodes. |
| Redis or Postgres made highly available | Needs extra replicas with their own storage and, for the anti-affinity to mean anything, at least as many nodes as replicas. |
| Cluster autoscaler enabled | Needs a node pool minimum of 2 for the same availability reasons, and a maximum that fits the vCPU quota. |
| Memory requests raised after the cluster benchmark | Check the new total against one node's allocatable memory, not the whole cluster. |

## Checking on the cluster

The numbers above are computed from requests, not measured on AKS. After a deploy:

```sh
kubectl describe node | grep -A8 "Allocatable\|Allocated resources"
kubectl top node
kubectl get pods -n poll -o wide
```

- **Allocated resources** shows what is requested on each node, including the system pods. The poll namespace should be well under half of one node's allocatable CPU and memory.
- `kubectl top node` shows real usage. Sustained CPU near 100% on a node means the B-series credits are running out.
- `kubectl get pods -o wide` should show each of Poll, Result and Worker split across both nodes.
