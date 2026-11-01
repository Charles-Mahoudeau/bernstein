# Replica counts

How many pods of each component the Poll app runs in `charts/poll-app/values.yaml`, and why.

| Component | Replicas | Why                                                                                                                  |
|-----------|----------|----------------------------------------------------------------------------------------------------------------------|
| Poll      | 2        | Stateless and user-facing. Two replicas keep one serving during a pod loss, a node drain or a rolling update.        |
| Result    | 2        | Same as Poll.                                                                                                        |
| Worker    | 2        | Stateless queue consumer. Two replicas keep votes flowing if one dies; the PodDisruptionBudget keeps one up on drain. |
| Redis     | 1        | A second replica would be a separate, empty queue. Real replication needs Sentinel, which is out of scope.           |
| Postgres  | 1        | A second replica would have its own data. The PVC keeps data across restarts; the StatefulSet recreates the pod.     |

The benchmark in [resource-limits.md](resource-limits.md#benchmark) was run with one Poll and one Result and shows that one replica is enough for the load but not for availability:

- **Capacity:** one Poll replica handled 338 votes/s with 0 failed votes, using about 85% of a core. One Result served 500 live viewers using 49 MiB. Two replicas double that headroom, far above what this project needs.
- **Availability:** the benchmark measures load, not failures. Zero downtime during a rolling update, a pod loss or a node drain (see [scripts.md](scripts.md)) needs a second replica already serving, whatever the load.
- **Worker:** at 2% CPU each, the workers drain the queue immediately, so two are for availability, not throughput.

Two is the smallest count that tolerates losing one pod or one node, and it is enough for this project: the application is a demo with a few users. A third replica would cost cluster resources without improving what the tests check. With these counts the total requests are 1.15 CPU and about 1.3 GiB. A single `B2s_v2` node has 1.9 CPU allocatable, shared with kube-system, so after a drain it does not hold everything, and Redis and Postgres take priority over a second replica ([node-count.md](node-count.md#after-losing-a-node)).

Redis and Postgres are not highly available: while their single pod restarts, voting or results are briefly unavailable, but no data is lost (Redis is only a queue, Postgres has a persistent volume). Poll and Result stay up through the ingress.

## Spread and disruption budgets

Two replicas only help if they are on different nodes and a drain cannot evict both at once. Each of Poll, Result and Worker therefore has:

- a `PodDisruptionBudget` with `minAvailable: 1` (`templates/<component>/pdb.yaml`): `kubectl drain` and AKS node upgrades evict one replica, wait for it to be rescheduled and ready, then evict the next;
- a `topologySpreadConstraints` entry (`maxSkew: 1` on `kubernetes.io/hostname`, `matchLabelKeys: [pod-template-hash]`) so the replicas of the current revision land on different nodes.

The constraint uses `ScheduleAnyway`, a preference: a single-node cluster still schedules, and a rollout is never blocked by a full node. Check the placement with `kubectl get pods -n poll -o wide`. A PDB needs at least two replicas: with one, `minAvailable: 1` can never be satisfied and a drain hangs.

The replica counts of Poll and Result are not fixed: a HorizontalPodAutoscaler keeps Poll between 2 and 4 and Result between 2 and 3 ([autoscaling.md](autoscaling.md)). `poll.replicas` and `result.replicas` only apply when the component's `autoscaling.enabled` is `false`. With 2 or more Result pods, the Result Service needs a sticky cookie so Socket.IO long-polling keeps working. The minimum of 2 comes from this page.
