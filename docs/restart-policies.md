# Restart policies

Notes on how Kubernetes restarts containers, and the policy chosen for each workload of the Poll app.

## What Kubernetes offers

`restartPolicy` is a Pod-level field, applied by the kubelet to the containers of that pod, on the node where it runs.

| Value               | Behavior                                                         | Allowed on                                                           |
|---------------------|------------------------------------------------------------------|----------------------------------------------------------------------|
| `Always` (default)  | Restart the container whenever it exits, even with exit code 0   | Deployment, StatefulSet, DaemonSet, ReplicaSet (this value only)     |
| `OnFailure`         | Restart only on a non-zero exit code                             | Job, CronJob, bare Pods                                              |
| `Never`             | Never restart in place; the pod ends `Succeeded` or `Failed`     | Job, CronJob, bare Pods                                              |

### Deployment vs Job

A Deployment's controller keeps the desired number of pods alive forever, so it only accepts `Always`. A Job runs to completion and uses `OnFailure` or `Never`:

- With `OnFailure` the same pod restarts in place, and its previous logs are overwritten.
- With `Never` the Job creates a new pod per retry, so failed pods stay around for debugging.
- `backoffLimit` (default 6) and `activeDeadlineSeconds` bound the retries at the Job level.

### CrashLoopBackOff

CrashLoopBackOff is not a policy. It is the kubelet's exponential delay between restarts of the same container: 10s, 20s, 40s and so on, capped at 5 minutes, and reset once the container has run 10 minutes without crashing. Recent Kubernetes versions have a feature gate that shortens this delay, so check the cluster version before relying on exact numbers.

### Init and sidecar containers

Init containers follow the pod policy, except that `Always` behaves as `OnFailure` for them. Since Kubernetes 1.29, an init container with its own `restartPolicy: Always` is a native sidecar.

### Probes

Probes decide when a restart (or a traffic change) happens.

| Probe     | On failure                                                          |
|-----------|---------------------------------------------------------------------|
| liveness  | The container is killed and restarted, subject to `restartPolicy`   |
| readiness | The pod is removed from Service endpoints; nothing is restarted     |
| startup   | Liveness and readiness are held back until it succeeds; if it never does, the container is killed |

An over-eager liveness probe turns a slow dependency into a restart loop, so liveness should only check that the process itself is alive.

## Policy applied to each workload

Every workload is long-running, so `Always` is the only valid choice. The chart has no Job or CronJob. The policy is written explicitly in every template so the decision is visible.

| Workload | Kind                    | `restartPolicy` |
|----------|-------------------------|-----------------|
| poll     | Deployment              | `Always`        |
| result   | Deployment              | `Always`        |
| worker   | Deployment              | `Always`        |
| redis    | Deployment (`Recreate`) | `Always`        |
| postgres | StatefulSet             | `Always`        |

## Probes review

### poll, result, worker

Liveness is `GET /healthz`, which returns a static 200 in all three apps. It does not touch Redis or PostgreSQL, so a backend outage never restarts the app pods. Readiness is `GET /readyz`, which does check the dependencies (Redis for poll, PostgreSQL for result, both for worker). During an outage the pods leave the Service endpoints and come back by themselves when the backend recovers.

### Fatal exits in the apps

Some failures bypass the probes and end the process directly, which `restartPolicy: Always` turns into a restart:

- `result` calls `process.exit(1)` on a lost PostgreSQL connection or a failed query. After a PostgreSQL restart, `result` therefore crashes and restarts until the database is back, and goes through CrashLoopBackOff if the outage lasts. The app retries the initial connection up to 1000 times, which covers startup but not a later loss.
- `worker` exits with code 1 when interrupted while sleeping and when the JDBC driver is missing.

This is acceptable: the restart is the recovery mechanism, and readiness keeps traffic away meanwhile.

### Graceful shutdown

On a rollout, node drain or scale down, the kubelet sends SIGTERM and waits `terminationGracePeriodSeconds` (30s by default) before SIGKILL.

- `poll` runs gunicorn, which handles SIGTERM and finishes in-flight requests.
- `worker` runs the JVM directly, which exits on SIGTERM.
- `result` runs `node server.js` as PID 1 with no SIGTERM handler. A process running as PID 1 does not get the default signal action, so SIGTERM is ignored and the pod is killed after the full 30s. This slows every rollout of `result`, and the Socket.IO connections are cut instead of closed cleanly. Adding a `process.on('SIGTERM', ...)` handler that closes the server fixes it (an `init: true`-style wrapper such as tini would also work).

### redis

Liveness and readiness both use `redis-cli ping`. Redis has no persistence configured, so a restart or reschedule loses the queued votes that the worker has not consumed yet. This is a known trade-off of the current setup, not something a restart policy can change.

### postgres

PostgreSQL does not write every change straight into its data files. It first appends a short record of the change to a log, the WAL (Write-Ahead Log), and updates the data files later in batches. After a hard kill or a node loss, the data files may lack changes that were already acknowledged, so on the next start PostgreSQL replays the WAL before it accepts connections ("database system was not properly shut down; automatic recovery in progress"). This takes seconds to minutes depending on how much log has accumulated. During recovery the server is running but `pg_isready` fails.

Liveness uses `pg_isready` with `initialDelaySeconds: 30`, `periodSeconds: 10` and `failureThreshold: 6`, so the first kill can happen about 90s after start. If recovery takes longer than that, the kubelet kills the container mid-recovery, the next start has to begin recovery again, and it can loop.

A `startupProbe` fixes this. It runs first and keeps liveness switched off until it succeeds, after which liveness applies with tight settings. With a generous budget, for example `periodSeconds: 10` and `failureThreshold: 30` (5 minutes), PostgreSQL gets time to recover without being killed, and liveness no longer needs the long initial delay. With our small dataset, recovery takes seconds, so the risk here is low and the probe is a robustness improvement rather than a fix.

## Possible follow-ups

- Add a `startupProbe` to postgres (and optionally redis).
- Handle SIGTERM in `result/server.js`.
- Decide whether redis needs persistence, or document the loss as accepted.
