# Drains and node loss

What the chaos runs of 2026-11-01 showed about a node drain, a node power-off and a release, and the chart changes made because of them. Reports are in `reports/` (not versioned). All times are seconds after the operation starts.

## What was measured

**Node power-off** (`chaos node-kill`, report `183256`). Two separate failure windows:

- 4 to 21 s, on all three targets (poll, result, Socket.IO): the Azure load balancer kept sending connections to the dead node until its health probe failed. Traefik had a pod on that node.
- 22 to 70 s, on `poll` only, one failed request about every 2.2 s: the dead `poll` pod stayed in the Service endpoints until Kubernetes declared the node `NotReady` (the controller waits `node-monitor-grace-period`, 40 s, then marks the pods not ready; the report shows the replica count dropping at 69.8 s). Traefik sends requests straight to pod IPs from the endpoints and has no health check of its own, so every second request went to the dead pod and waited for the 30 s default dial timeout.

**Node drain** (`chaos drain`, reports `191456`, `192502`, `193354`). Poll had 0 failures. `result` and Socket.IO failed for 30 to 50 s (150 to 260 failed requests), with HTTP 503 once Traefik had no ready `result` pod. Cause, from the pod events and `kubectl logs --previous`:

1. The drained node held Postgres in all three runs. Postgres is a single instance on an Azure disk (RWO): the new pod waited for the disk to detach from the old node (`FailedAttachVolume`, about 18 s) before it could start, so Postgres was down for 22 to 31 s.
2. `result` calls `process.exit(1)` when its PostgreSQL connection drops (`server.js`, see [restart-policies.md](restart-policies.md#fatal-exits-in-the-apps)). Both pods restarted and stayed unready until Postgres was back.
3. The first readiness probe after Postgres returned still failed, and the next one came 10 s later.

**Node drain without Postgres** (`chaos drain`, report `194957`, after the changes below). The drained node held Redis and one replica of each other component. Poll and Result had 0 failures, Socket.IO 1 failed request (an HTTP 502 at 32.8 s) out of about 5600 across the three targets. Postgres never went down, so `result` did not restart, and a `result` and a `poll` pod were always ready. Redis was down for 6.7 s and the workers for 8.9 s; votes are not sent by the probe, so a vote in that window is not covered by this number. This is the case the PDBs and spread constraints are meant to guarantee.

The run still ended with `operation interrupted` after 300 s: with `DoNotSchedule` and one schedulable node, the evicted Poll, Result, Worker and Traefik pods stay Pending until the uncordon, so `wait_app_ready` (300 s timeout) never sees every workload healthy. The cluster was healthy for users throughout.

**Release** (`release`, report `194059`). The release recreated Postgres (the StatefulSet pod template had changed, see below), so it caused the same outage as a drain: 149 failed requests out of 259 on `result`, none on `poll`. The next release (report `194243`), which did not touch Postgres, ran with 0 failures.

## Changes and why

| Change | Where | Why |
|---|---|---|
| `DoNotSchedule` instead of `ScheduleAnyway` | `poll`, `result`, `worker` Deployments, Traefik (`traefik-values.yaml`) | With `ScheduleAnyway`, a drain puts every replica on the remaining node, and nothing moves them back. The next node loss then removes all replicas of a component. This happened: after a drain, a power-off hit a node that held the whole app and everything was down for 400 s. With `DoNotSchedule` two replicas can never share a node. |
| `matchLabelKeys: [pod-template-hash]` | same four | Only pods of the revision being placed are counted. Without it, a rollout balances the new pods against the old ones: if both old pods are on node A, the first new pod goes to B, but once one old pod is deleted both nodes have one pod and the second new pod can go to either. With the key, the second new pod must go to the other node. Poll and Result already had it, Worker and Traefik did not. |
| Retry middleware (3 attempts, 100 ms, doubling) and `ServersTransport` with a 2 s dial timeout | `templates/traefik.yaml`, annotations on the `poll` and `result` Services and Ingresses | Kubernetes cannot remove a dead pod from the endpoints before about 40 s, so Traefik must cope with it. A 2 s dial timeout turns a 30 s hang into a quick failure, and the retry sends the request to another pod. The retry only applies to connections that failed before the request reached a pod, so a vote is not sent twice. Needs the Traefik CRDs, hence Traefik must be installed before the app. `traefik.resilience.enabled: false` turns it off. |
| `result` readiness: `periodSeconds` 10 to 3, `timeoutSeconds` 2 | `values.yaml` | `/readyz` is one `SELECT 1`, so a short period is cheap. After a Postgres restart the pods become Ready at the next probe, which was up to 10 s later. Leaving the Service takes 9 s instead of 30 s. |
| Postgres startup and readiness `periodSeconds` 5 to 2 (`failureThreshold` 60 to 150, still 5 minutes) | `templates/postgres/statefulset.yaml` | Saves at most 3 s. This change turned out to cost more than it brings: modifying the StatefulSet pod template recreates Postgres, which is how report `194059` got its outage. It is kept because it is deployed, but see the rule below. |

## Rules that follow

- **Do not modify the Postgres StatefulSet in a release that is meant to be zero-downtime.** Any change to its pod template recreates the pod, and the disk reattach plus the `result` crash cost about 30 s of `result` downtime. Check with `kubectl get controllerrevision -n <ns> -l app.kubernetes.io/component=postgres`: the revision number must not change.
- **A drain of the node that holds Postgres cannot be zero-downtime** with a single instance on an RWO disk. To test the drain guarantees, drain a node without Postgres (`kubectl get pods -o wide`, then `bernstein chaos drain --node <other>`). Redis, if on the drained node, adds about 7 s without Redis.
- **Check the spread before a chaos run** (`kubectl get pods -o wide`, one pod of each component per node). Pods are not moved after a drain: restart the Deployments (`kubectl rollout restart`) once the node is back.

## Not fixed

- The first window of the power-off (4 to 21 s, load balancer still sending to the dead node) is in front of Traefik, so the retry does not help. It would need the Traefik Service health probe settings, or `externalTrafficPolicy: Local`, which has the opposite problem (see [deployment-choices.md](deployment-choices.md#load-balancing)).
- `result` exiting on a lost database connection can only be fixed in `server.js`, which is out of scope. The chart only shortens the recovery.
- The Postgres downtime itself (disk detach and attach, about 20 s) would need a replicated database.
- `chaos drain` used to wait the full 300 s and fail, because `DoNotSchedule` keeps the evicted replicas Pending while the node is cordoned. It now waits only until every workload has one ready replica (`wait_app_ready(min_one_replica=True)` in `scripts/src/bernstein/kube.py`), and every `chaos` command now defaults to `--min-availability 99` instead of 100, since a drain of the Postgres node or a node loss cannot do better than a 20 to 30 s `result` outage. `release`, `rollback` and `probe` keep 100.
- The single Socket.IO 502 at 32.8 s of the last drain is not explained: the report does not say which pod served it.
