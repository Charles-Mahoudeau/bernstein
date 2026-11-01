# Resource requests and limits

Why every component of the Poll app has the CPU and memory settings it has in `charts/poll-app/values.yaml`, and the measurements behind them.

- [Policy](#policy)
- [Values](#values)
- [Benchmark](#benchmark)
- [Reading the results](#reading-the-results)
- [Limits of the benchmark](#limits-of-the-benchmark)
- [Re-measuring on the cluster](#re-measuring-on-the-cluster)

## Policy

- **Memory request: above the measured steady usage.** Memory cannot be throttled. A pod that regularly uses more than it requested is under-counted by the scheduler and is the first one evicted when a node runs short.
- **Memory limit: 2 to 4 times the measured peak, or at least the runtime's own needs.** A leak or a traffic spike kills one pod (`OOMKilled`, restarted, covered by the other replicas and the PodDisruptionBudget) instead of starving its neighbours. The headroom keeps normal load far from that line.
- **CPU request only, no CPU limit.** A CPU limit throttles the container even when the node is idle. That hurts the JVM at startup and every request on Poll, and it would show up as failures in the availability probe during rollouts. The request is only used for scheduling and for the share of CPU under contention.
- **Result: higher CPU request.** It pushes an update to every connected browser each second, so it should keep a larger CPU share than idle pods such as Redis if the node gets busy.
- **Postgres: Guaranteed QoS, the one exception to "no CPU limit".** It is a single replica holding the data, so no other pod covers for it if it is evicted. Setting request = limit for CPU and memory makes it the last pod evicted when a node runs short of memory. The cost is a fixed CPU ceiling (200m, about 8 times the measured peak) and a larger reservation on its node.
- **LimitRange as a safety net.** `templates/limitrange.yaml` gives any container that declares no resources a request of 50m / 64Mi and a memory limit of 128Mi, and rejects containers above 1 CPU or 1Gi. Every component below sets its own values, so it only catches future additions that forget to.
- **No ResourceQuota.** The app has its own namespace, so there is no neighbour to protect the cluster from. A quota would also be a risk: a rolling update briefly runs surge pods, and a quota that is too tight would refuse them and stall the rollout.

Every pod except Postgres ends up in the Burstable QoS class (request below limit), which is acceptable because they have replicas, readiness probes and PodDisruptionBudgets. Guaranteed QoS needs request = limit for CPU and memory, which means a CPU limit, so it is only worth its cost for Postgres.

| QoS class | Rule | Evicted | Used by |
|---|---|---|---|
| Guaranteed | request = limit for CPU and memory | last | postgres |
| Burstable | some request or limit set, not Guaranteed | second | poll, result, worker, redis |
| BestEffort | nothing set | first | none |

## Values

The cluster is 2 × `Standard_B2s_v2` (2 vCPU, 8 GiB each). Total requests are 1.15 CPU and about 1.3 GiB across all pods at the minimum replica counts (replica counts: [replica-counts.md](replica-counts.md)), and 1.65 CPU when Poll has scaled to 4 pods ([autoscaling.md](autoscaling.md)). Each node has 1.9 CPU allocatable, and on 2026-10-31 the nodes already carried 1.07 and 1.29 CPU of requests, kube-system included. Whether one node holds everything after a drain was not tested. It is tight at the minimum counts, and with 4 Poll pods a drain would probably leave a pod Pending.

| Component | Replicas | CPU request | Memory request | Memory limit | Why |
|---|---|---|---|---|---|
| poll | 2 to 4 | 250m | 96Mi | 192Mi | Flask + gunicorn, one process with 4 threads. Measured peak 63MiB. The CPU request is the base of the autoscaler target, see [autoscaling.md](autoscaling.md). |
| result | 2 to 3 | 100m | 128Mi | 256Mi | Node 24 + Socket.IO. Memory grows with connected viewers: 49MiB at 500. |
| worker | 2 | 100m | 128Mi | 512Mi | JVM. Its max heap is 25% of the limit (128MiB), plus metaspace and thread stacks. Measured peak 65MiB, so the request is about twice the usage. |
| redis | 1 | 50m | 64Mi | 128Mi | Holds the vote queue, which the workers drain immediately. Measured peak 13MiB. |
| postgres | 1 | 200m (limit 200m) | 512Mi | 512Mi | Guaranteed QoS, so request = limit. `shared_buffers` defaults to 128MB and must fit under the limit. Measured peak 44MiB on a small table. |

## Benchmark

Done on 2026-10-31. The AKS cluster was not running, so this was run locally with Docker, using the **real images built from `poll-application/`**, the **same images for Redis and Postgres as the chart** (`redis:8.10.2-alpine`, `postgres:18.6-alpine`), the **same memory limits as the table above** (`docker run --memory`), no CPU limit, and 2 worker containers. The `votes` table was created from `charts/poll-app/files/schema.sql`.

Load generator: a Python script (httpx threads for voting, python-socketio clients for viewers). Every vote uses a new `voter_id`, so every vote is a real row in Postgres. A run counts as correct only if the number of rows in Postgres equals the number of accepted votes and the Redis queue is empty at the end. Memory and CPU were sampled with `docker stats` about once per second.

### Idle

| Container | Memory |
|---|---|
| poll | 52 MiB |
| result | 24 MiB |
| worker (each) | 45 to 47 MiB |
| redis | 11 MiB |
| postgres | 35 MiB |

### Under load

| | Moderate | Heavy |
|---|---|---|
| Concurrent voters | 20 | 100 |
| Result viewers (Socket.IO) | 100 | 500 |
| Duration | 60 s | 90 s |
| Votes accepted / failed | 18,876 / 0 | 30,427 / 0 |
| Throughput | 315 votes/s | 338 votes/s |
| Vote latency p50 / p99 | 42 ms / 155 ms | 210 ms / 709 ms |
| Rows in Postgres at the end | 18,876 | 30,427 |
| Redis queue at the end | 0 | 0 |
| OOMKilled / restarts | none | none |

Peak memory and CPU per container (CPU is a percentage of one core):

| Container | Limit | Moderate: memory | Moderate: CPU | Heavy: memory | Heavy: CPU |
|---|---|---|---|---|---|
| poll | 192 MiB | 62 MiB | 87% | 63 MiB | 85% |
| result | 256 MiB | 31 MiB | 3% | 49 MiB | 7% |
| worker 1 | 512 MiB | 61 MiB | 2% | 61 MiB | 2% |
| worker 2 | 512 MiB | 65 MiB | 2% | 65 MiB | 2% |
| redis | 128 MiB | 12 MiB | 7% | 13 MiB | 6% |
| postgres | 512 MiB | 42 MiB | 8% | 44 MiB | 8% |

## Reading the results

- **No limit came close to being hit.** The highest memory use relative to its limit is poll at 33% (63 of 192 MiB). Nothing was OOM-killed and no vote was lost or duplicated.
- **Result grows with viewers, but slowly.** It went from 24 MiB idle to 31 MiB with 100 viewers and 49 MiB with 500 viewers, about 50 KiB per connection. The 256 MiB limit holds roughly 4,000 viewers by that trend. That is more than the project needs, and cheap on this cluster. The previous 128 MiB limit would also have survived this benchmark, so the increase is deliberate headroom, not a fix for an observed problem.
- **The JVM request is about twice its usage.** The workers peaked at 65 MiB against a 128 MiB request, which leaves room for JVM warmup. The 512 MiB limit stays, so a larger heap still fits if the workload grows.
- **Redis and Postgres are over-provisioned by 8 to 10 times.** Redis only holds votes until a worker takes them, and the table is tiny. The 128 MiB and 512 MiB limits protect against a runaway queue (for example if the workers are down for a while) and a growing table, not against current use.
- **Poll is the bottleneck, and CPU is the reason, not memory.** One gunicorn sync worker uses about 85% of a core and caps throughput near 330 votes/s. The latency under heavy load (p99 709 ms) is queueing in that single worker. The 50m CPU request does not limit it, because there is no CPU limit and the node has spare cores. If Poll needs to take more load, add gunicorn workers (`--workers`) or replicas. Raising memory would not help. This benchmark predates issue #40: Poll now runs 4 gunicorn threads, see [autoscaling.md](autoscaling.md#overload-and-probes-issue-40).
- **The workers keep up easily.** At 2% CPU each, the queue never builds and the Result tally follows the votes.

## Limits of the benchmark

- It ran on a 16-core desktop with Docker, not on AKS. Absolute CPU numbers and latencies will differ on 2 vCPU burstable VMs (`B2s_v2`), and the network path through Traefik is missing. Memory figures should carry over, since the images and limits are the same.
- The runs are 60 to 90 seconds. A slow leak would not show. The first minutes of a JVM and a Node process are not representative of days of uptime.
- Postgres had at most 30,000 rows. A larger table and longer uptime increase its page cache use, which counts toward the container limit, so its 512 MiB limit should be re-checked if the data grows by orders of magnitude.
- The load generator is Python and was probably close to its own limit in the heavy run, so 338 votes/s is a floor for Poll, not a precise ceiling.

## Re-measuring on the cluster

After a deploy, put the app under load and read the real usage:

```sh
kubectl top pod -n poll --containers
kubectl get pods -n poll -o custom-columns=NAME:.metadata.name,RESTARTS:.status.containerStatuses[*].restartCount,LAST:.status.containerStatuses[*].lastState.terminated.reason
```

`OOMKilled` in the second command means a memory limit is too low. If the usage in `kubectl top` is above the memory request for a component, raise the request.
