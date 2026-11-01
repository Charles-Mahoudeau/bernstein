# Traefik

How Traefik is installed and configured, which Traefik resources the chart creates (middleware, transport, annotations), and why. The measurements behind the resilience settings are in [node-failure.md](node-failure.md); the path of a request from the internet is in [deployment-choices.md](deployment-choices.md#load-balancing).

## Role

Traefik is the ingress controller: the only component reachable from outside. The Azure load balancer sends each connection to a node, the `traefik` Service forwards it to a Traefik pod, and Traefik matches the Ingress host (`poll.<ip>.nip.io` or `result.<ip>.nip.io`) and proxies to a pod of the `poll` or `result` Service. It reads the pod IPs from the EndpointSlices (the ClusterIP is not used) and balances in round-robin among the Ready pods.

Traefik was chosen because ingress-nginx is retired (March 2026) and Gateway API adds resource types for little gain here ([deployment-choices.md](deployment-choices.md#choices)).

## Two installs

| What | Installed by | Config | Namespace |
|---|---|---|---|
| Traefik itself (official chart `traefik/traefik`) | `bernstein traefik install` | `charts/traefik-values.yaml` | `traefik` |
| Middleware, transport and annotations for the app | `bernstein deploy` (the `poll-app` chart) | `traefik.resilience` in `charts/poll-app/values.yaml` | `poll` |

Traefik must be installed first: the `Middleware` and `ServersTransport` kinds are CRDs shipped with its chart, so the app chart fails to install without them.

## Traefik deployment (`charts/traefik-values.yaml`)

| Setting | Value | Why |
|---|---|---|
| `deployment.replicas` | 2 | With one pod, losing or draining its node cuts Poll and Result until it restarts |
| `topologySpreadConstraints` | `maxSkew: 1` on `kubernetes.io/hostname`, `DoNotSchedule`, `matchLabelKeys: [pod-template-hash]` | Two pods on one node fail together. `ScheduleAnyway` let a drain pile everything on one node, and nothing moved it back. `matchLabelKeys` makes a rollout spread the new pods instead of balancing them against the old ones |
| `podDisruptionBudget` | `minAvailable: 1` | A drain never evicts the last running Traefik |
| `service.spec.externalTrafficPolicy` | `Cluster` | Every node accepts traffic and forwards it to a Traefik pod. `Local` keeps the client IP but the Azure health probe takes seconds to notice a node lost its Traefik, and requests sent there fail during a drain. The app does not use the client IP |

The cost of `DoNotSchedule`: during a drain, the evicted Traefik pod stays Pending until the node is back, so one Traefik serves alone meanwhile (the PDB keeps it running).

## Resources created by the app chart

All are created from `charts/poll-app/templates/traefik.yaml` and wired with annotations. Names come from the helpers in `_helpers.tpl`; annotations reference them as `<namespace>-<name>@kubernetescrd`. Everything in this section is skipped with `traefik.resilience.enabled: false`.

### Retry middleware

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
spec:
  retry:
    attempts: 3          # traefik.resilience.retry.attempts
    initialInterval: 100ms   # doubles at each attempt
```

Attached to both Ingresses (`poll` and `result`) with the annotation `traefik.ingress.kubernetes.io/router.middlewares`.

**Why.** Traefik has no health check of its own: it trusts the endpoints. When a node dies, its pods stay in the endpoints until Kubernetes marks the node `NotReady` (`node-monitor-grace-period` 40 s, plus propagation). Meanwhile round-robin sends a share of the requests to a dead IP. The retry sends such a request to another pod, so the user sees a short delay instead of an error.

**Why it is safe for votes.** Traefik's retry only applies when the connection failed before the request was sent to a backend (no response, no byte forwarded). A request that reached a pod is never replayed, so a `POST /vote` is not counted twice.

### ServersTransport (dial timeout)

```yaml
apiVersion: traefik.io/v1alpha1
kind: ServersTransport
spec:
  forwardingTimeouts:
    dialTimeout: 2s      # traefik.resilience.dialTimeout
```

Attached to both Services with `traefik.ingress.kubernetes.io/service.serverstransport`. It applies to the connection from Traefik to the pod only; response and idle timeouts keep their defaults, so slow requests and Socket.IO long-polling are unaffected.

**Why.** The default dial timeout is 30 s. Without the transport, the first request sent to a dead pod hung for 30 s before the retry could even start. With 2 s, the failure is quick (the measured failure rate on `poll` after a power-off was one request every 2.2 s, each waiting 30 s). Pods in the cluster accept connections in milliseconds, so 2 s is far above normal and still short for a user.

The two work together: the timeout makes the failure fast, the retry makes it invisible. With 3 attempts, a request fails only if three picks in a row hit dead pods.

### Sticky cookie on Result

`result.service.stickyCookie: true` adds `traefik.ingress.kubernetes.io/service.sticky.cookie: "true"` to the Result Service. The Result page uses Socket.IO long-polling, and a session lives in the memory of the pod that opened it. A request landing on another replica is rejected (`Session ID unknown`), so without the cookie two replicas break the live tally. Poll needs none: it keeps no server-side state (the `voter_id` cookie is stored by the browser).

## Settings

| Value (`poll-app`) | Default | Effect |
|---|---|---|
| `traefik.resilience.enabled` | `true` | Create the middleware and transport and the annotations that use them |
| `traefik.resilience.dialTimeout` | `2s` | Traefik to pod connection timeout |
| `traefik.resilience.retry.attempts` | `3` | Total tries per request |
| `traefik.resilience.retry.initialInterval` | `100ms` | Wait before the first retry, doubled each time (100, 200 ms) |
| `result.service.stickyCookie` | `true` | Pin a browser to one Result pod |
| `poll.ingress.className`, `result.ingress.className` | `traefik` | Which controller serves the Ingress |
| `poll.ingress.host`, `result.ingress.host` | empty | Host to match; empty matches any, the CLI sets `<component>.<ip>.nip.io` |

## Limits

- The retry does not help for the first seconds of a node power-off (4 to 21 s): the Azure load balancer still sends connections to the dead node, in front of Traefik. See [node-failure.md](node-failure.md).
- Only connection failures are retried. A pod that accepts the connection and then dies mid-request returns an error to the user (HTTP 502).

## Checking

```bash
kubectl -n poll get middleware,serverstransport
kubectl -n poll get ingress -o yaml | grep router.middlewares
kubectl -n poll get svc -o yaml | grep -E 'serverstransport|sticky'
kubectl -n traefik get pods -o wide        # 2 pods, on different nodes
kubectl -n traefik get pdb
```
