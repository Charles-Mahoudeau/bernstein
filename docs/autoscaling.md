# Autoscaling

How Poll and Result scale with load, why the other components do not, and the load tests that show it working on AKS.

- [What scales](#what-scales)
- [Settings](#settings)
- [Load test on AKS: Poll](#load-test-on-aks-poll)
- [Load test on AKS: Result](#load-test-on-aks-result)
- [Overload and probes](#overload-and-probes-issue-40)
- [Limits](#limits)
- [Node autoscaling](#node-autoscaling)
- [Reproducing the test](#reproducing-the-test)

## What scales

**Poll** and **Result**, the two user-facing components, each have a HorizontalPodAutoscaler. Both come from one template, `poll-app.hpa` in `templates/_helpers.tpl`, used by `templates/poll/hpa.yaml` and `templates/result/hpa.yaml`. The benchmark in [resource-limits.md](resource-limits.md#benchmark) and the AKS tests below decide who gets one:

| Component | CPU under load | HPA |
|---|---|---|
| poll | 85% of a core at 330 votes/s | yes, the CPU-bound component. Scales first. |
| result | about 55m per pod with 1,100 viewers on AKS | yes, but it has a lot of headroom and is hard to trigger. Existing viewers stay on their pod, see [Limits](#limits). |
| worker | 2% | no, it drains the queue immediately |
| redis, postgres | 6 to 8% | no, single instance by design ([replica-counts.md](replica-counts.md)) |

## Settings

In `charts/poll-app/values.yaml`, under `poll.autoscaling` and `result.autoscaling`:

| Value | Poll | Result | Why |
|---|---|---|---|
| `minReplicas` | 2 | 2 | The floor from [replica-counts.md](replica-counts.md). The PodDisruptionBudget (`minAvailable: 1`) needs a second pod. |
| `maxReplicas` | 4 | 3 | A Poll pod can use about a core, and the cluster has 4 vCPU. Result needs far less, so 3 is enough. More pods would starve the other components. |
| `targetCPUUtilizationPercentage` | 70 | 70 | Of the CPU **request**. A Poll pod above 175m (250m request) or a Result pod above 70m (100m request) is over target. |
| `scaleDownStabilizationSeconds` | 120 | 120 | Waits two minutes of low CPU before removing a pod, so a short pause in traffic does not cause flapping. |

The HPA target is a share of the request, so the request matters. Poll's CPU request is **250m**, up from 50m. At 50m, a pod using 400m reads as 800% and the HPA would jump to the maximum on any traffic. 250m is a safe margin over the measured usage, not a tuned value. Result keeps its 100m request.

When `autoscaling.enabled` is `true`, the Deployment has no `spec.replicas`. Otherwise every `helm upgrade` would reset the count to `replicas`. Set it to `false` to go back to a fixed count.

## Load test on AKS: Poll

Measured on 2026-10-31 on the 2-node `Standard_B2s_v2` cluster, in the `poll-charles` namespace.

**CPU per pod, from the laptop** (2 Poll pods, 60 s per stage, 0 failed votes):

| Voters | Votes/s | p50 / p99 | CPU per Poll pod |
|---|---|---|---|
| 2 | 62 | 32 / 49 ms | about 20m |
| 5 | 155 | 31 / 69 ms | about 77m |
| 10 | 300 | 32 / 67 ms | about 160m |
| 20 | 405 | 52 / 101 ms | 300 to 370m |
| 40 | 412 | 97 / 207 ms | 365 to 396m |

CPU grows roughly linearly with load, at about 2 millicores per vote/s per pod. That matches the desktop benchmark (85% of a core at 330 votes/s).

**HPA under load**: a Job in the same namespace ran `hey` (64 connections, 240 s) against the Poll Service.

| Time | Event |
|---|---|
| +0 s | Load starts, 2 Poll pods. |
| about +80 s | CPU at 129% of target: the HPA scales **2 to 4**. |
| +240 s | Load ends. |
| about +60 s later | CPU at 6 to 11%. After the 120 s window the HPA scales **4 to 2**. |

The HPA scaled up under load and back down afterwards.

## Load test on AKS: Result

Same cluster and namespace. The load is Socket.IO viewers using the polling transport, like the browser page (`transports: ['polling']`), connected through the public Traefik ingress by a threaded `python-socketio` client on the laptop. Connections ramped by 100 every 10 s. About 38% of the connection attempts failed on the generator side, so the figures below count **connected** viewers.

| Viewers connected | Result CPU per pod (2 pods) | HPA |
|---|---|---|
| about 430 | 34 to 41m (40% of target) | stays at 2 |
| about 1,100 | 48 to 72m (52 to 65%) | stays at 2 |
| about 1,100 + a second generator of the same size | up to 123m and 104m (113%) | **2 to 3** at 92% |

After the viewers left, the average CPU fell below target and, after the 120 s window, the HPA went **3 to 2**.

- Memory stayed at 50 to 65 MiB per pod, far under the 256 MiB limit.
- **Result is hard to saturate.** 1,100 viewers fit on 2 pods under the target. That is more than this project needs, and the HPA is a safety net, not something a normal demo reaches.
- The third pod used only 16 to 20m while the other two were at 100m or more: see [Limits](#limits).

## Overload and probes

The first Poll test above showed the probes failing under overload. Poll ran one gunicorn sync worker, which serves one request at a time, so `/healthz` waited behind the queued votes, missed the 1 s probe timeout, and the kubelet restarted a pod that was only busy. Two chart changes fix it, with the same image:

- **gunicorn runs 4 threads**: `--worker-class gthread --threads 4`, set in `poll.gunicorn.args` and passed as `GUNICORN_CMD_ARGS`, so it can be tuned without a rebuild. Threads rather than workers: each worker is a full Python process (about 50 MiB, against a 96Mi request), and one process already covers the 250m CPU request.
- **The probes are less strict, liveness more than readiness**, see [restart-policies.md](restart-policies.md#poll-result-worker):

| Probe | `timeoutSeconds` | `failureThreshold` | Fails after |
|---|---|---|---|
| liveness | 1 → 5 | 3 → 6 | about 1 minute without an answer |
| readiness | 1 → 3 | 3 | about 30 s |

Measured on 2026-11-01 on the same cluster, with the app deployed in one namespace only (`poll-tristan`). Both runs used the `hey` Job from [Reproducing the test](#reproducing-the-test) (64 connections, 240 s, against the Service), pinned to the same node, and the same images (`bea807d`). Only the chart values differ.

| | Before | After |
|---|---|---|
| Failed liveness probes | 8 | 0 |
| Failed readiness probes | 10 | 0 |
| Restarts | 0 | 0 |
| Failed requests | 0 of 25,832 | 0 of 132,000 |
| Requests/s | 107 | 550 |
| Latency p50 / p99 | 219 ms / 5.19 s | 112 ms / 203 ms |
| CPU of a loaded Poll pod | about 320m | about 970m |
| Memory of a Poll pod (peak) | 56 MiB | 62 MiB |

- **The probes no longer fail, at five times the load.** Before, every failure was on one of the two pods sharing the node with the load generator. None reached 3 failures in a row, so nothing was restarted this time, unlike the first test: the old settings were at the limit.
- **Most of the throughput gain comes from keep-alive.** The sync worker closes the connection after every response, so `hey` opened a new connection and made a DNS lookup for every request. About 8% of the requests took 5, 10 or 15 s: DNS lookups timing out after 5 s and retried (`DNS-lookup` up to 10 s in the `hey` details), and the Poll pods sat at about 20m near the end of the run while `hey` waited. This is the likely cause of the 107 req/s of the first test as well. `gthread` keeps connections open, so each of the 64 `hey` connections was opened once. Traefik also keeps its connections to the pods open, so real traffic benefits too.
- **The two changes were measured together.** There was no run with only one of them, so the table does not say how much each contributes.
- **The pods added by the HPA got no traffic in the "after" run.** The HPA went from 2 to 4, but the 64 connections were already open to the first 2 pods, and the Service balances connections, not requests. The new pods stayed at 1 to 16m while the first two used a full core each, so the 550 req/s came from 2 pods. Traefik balances each request, so this is a limit of the test, not of the app.
- **A busy Poll pod now uses a full core.** In the "before" run the pods stayed near 320m, held back by the new connection and DNS lookup of every request. With keep-alive and 4 threads the process stays busy until the GIL caps it near one core. Poll has no CPU limit, so 4 busy pods could take most of the 3.8 CPU of the two nodes. This was not reached here, since only 2 pods were loaded.

## Limits

- **New pods report no metrics for about a minute.** After a deploy the HPA shows `cpu: <unknown>/70%` until metrics-server has scraped the new pods. It does not scale during that time.
- **Under overload the probes failed** with the first settings: one gunicorn sync worker and the default 1 s probe timeout. The kubelet restarted one pod after 3 failed liveness probes, and about 45 of 26,000 requests failed (resets and timeouts) during the run. Fixed by issue #40, see [Overload and probes](#overload-and-probes-issue-40).
- **More pods did not mean more throughput in the test.** The in-cluster run reached about 107 req/s with 4 pods, below the 246 req/s of the earlier run with 2. The load generator, Redis and 4 busy Poll pods share two 2 vCPU nodes, and a pod was restarting. Node CPU was not sampled during this run. The rerun for issue #40 reached the same 107 req/s and showed DNS lookups timing out on every new connection, which is the likely cause.
- **New Result pods only get new viewers.** A Socket.IO connection stays on its pod, so scaling up does not move existing viewers. The new pod filled slowly (16 to 20m against 100m or more on the others) and the average fell mainly because viewers left. Scaling down closes the connections on the removed pod, and the browser reconnects to another pod.
- **Result needs a sticky cookie with more than one replica.** The page uses Socket.IO long-polling, and every polling request of a session must reach the same pod. Without stickiness, 9 of 12 test viewers got connection errors and none received a steady stream of updates. The chart therefore sets `traefik.ingress.kubernetes.io/service.sticky.cookie` on the Result Service (`result.service.stickyCookie`, on by default). After this, 12 of 12 viewers received every update. This applies to the 2 fixed replicas too, whatever the HPA does.
- **A busy Poll pod uses a full core.** Since issue #40 Poll runs 4 gunicorn threads and has no CPU limit, so 4 busy pods could starve Redis, Result and the worker. See [Node autoscaling](#node-autoscaling).
- **The numbers are from short tests.** 240 s with `hey`, a laptop ramp of 60 s per stage, and viewer ramps of a few minutes. The 70% target and the 250m request are starting values.

## Node autoscaling

Not enabled. At 4 Poll pods the app requests 1.65 CPU, and the nodes have 3.8 CPU allocatable in total (1.9 each), of which 2.36 was already requested before the HPA scaled up (app at its minimum plus kube-system). The pods can also burst to a full core each. If the cluster is short on CPU, new pods stay Pending and the HPA cannot help. The AKS cluster autoscaler adds a node in that case:

```sh
az aks update --resource-group Bernstein --name bernstein-aks \
  --enable-cluster-autoscaler --min-count 2 --max-count 3
```

It costs a third `B2s_v2` while scaled up and takes a few minutes to add the node, so it is left as an option. The minimum of 2 matches [replica-counts.md](replica-counts.md).

## Reproducing the test

Use your own namespace and context, and check them first:

```sh
kubectl --context bernstein-aks -n poll-charles get hpa
```

Then run a one-off Job that sends votes to the Poll Service, and watch the HPA:

```sh
kubectl --context bernstein-aks -n poll-charles create job loadgen --image=williamyeh/hey -- \
  /hey -z 240s -c 64 -m POST -T application/x-www-form-urlencoded -d vote=a \
  http://poll-app-poll.poll-charles.svc.cluster.local/
kubectl --context bernstein-aks -n poll-charles get hpa poll-app-poll -w
kubectl --context bernstein-aks -n poll-charles logs job/loadgen
kubectl --context bernstein-aks -n poll-charles delete job loadgen
```

The votes are real rows in Postgres. The Service name uses the namespace, so change `poll-charles` if you deploy elsewhere. Read the `hey` summary with `logs` before deleting the Job: deleting it removes its pod and the output.

Poll keeps connections open since issue #40, so through the Service the pods added by the HPA get no traffic from `hey`. To see them take load, send it through the ingress (`poll.<ip>.nip.io`), which Traefik balances per request.

For Result, open polling-only Socket.IO viewers against the **ingress** host (`result.<ip>.nip.io`), not the Service. The sticky cookie is set by Traefik, and polling through the plain Service alternates between pods and fails. Watch `kubectl ... get hpa poll-app-result -w`. About 2,000 connected viewers were needed to cross the target.
