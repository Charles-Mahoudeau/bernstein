"""Disruption tests, each run under the availability monitor.

Every test restores the cluster in a ``finally`` block (uncordon the node, start
the VM again), so an error or Ctrl-C does not leave it degraded.
"""

from __future__ import annotations

import random
import time
from enum import StrEnum
from typing import TYPE_CHECKING

from . import azure, console, helm, kube
from .config import CHART_DIR, shown_path
from .errors import BernsteinError, CommandFailedError
from .probe import Monitor
from .runner import run, state

if TYPE_CHECKING:
    from .config import Settings
    from .probe import Report

BROKEN_READINESS_PATH = "/bernstein-broken-readiness"


class BadReleaseMode(StrEnum):
    PROBE = "probe"
    IMAGE = "image"


def _ready_node_count(settings: Settings) -> int:
    return sum(1 for n in kube.nodes(settings) if n.ready and not n.unschedulable)


def delete_pods(
    settings: Settings,
    *,
    component: str | None,
    every_pod: bool,
    count: int,
    interval: float,
    min_availability: float,
) -> Report | None:
    """Delete one random pod (or all pods of a component), ``count`` times."""
    if every_pod and not component:
        raise BernsteinError("--all needs --component (deleting every pod is a full outage)")
    label = component or "random"
    with Monitor(settings, f"chaos pod {label}", min_availability=min_availability) as monitor:
        for round_number in range(1, count + 1):
            candidates = [p for p in kube.pods(settings, component=component) if not p.terminating]
            if not candidates:
                raise BernsteinError(f"no running pod found for component {label}")
            victims = candidates if every_pod else [random.choice(candidates)]
            console.step(f"Round {round_number}/{count}: deleting {len(victims)} pod(s)")
            for pod in victims:
                run(kube.kubectl(settings, "delete", "pod", pod.name, "--wait=false"))
                monitor.event(f"deleted pod {pod.name} ({pod.component}) on {pod.node}")
            time.sleep(0 if state.dry_run else 3)  # let the controller notice the deletion
            kube.wait_app_ready(settings)
            monitor.event("all workloads ready again")
            if round_number < count and not state.dry_run:
                time.sleep(interval)
    return monitor.report


def _pick_node(settings: Settings, name: str | None) -> kube.Node:
    candidates = kube.nodes(settings)
    if name:
        node = next((n for n in candidates if n.name == name), None)
        if node is None:
            raise BernsteinError(f"node {name} not found")
        return node
    # The node running the most application pods is the most interesting one to lose.
    load = {n.name: 0 for n in candidates if n.ready}
    for pod in kube.pods(settings):
        if pod.node in load:
            load[pod.node] += 1
    if not load:
        raise BernsteinError("no Ready node found")
    busiest = max(load, key=lambda node_name: load[node_name])
    console.info(f"picked {busiest} ({load[busiest]} app pods); use --node to choose")
    return next(n for n in candidates if n.name == busiest)


def _check_spare_capacity(settings: Settings) -> None:
    if _ready_node_count(settings) < 2:
        console.warn("fewer than 2 schedulable nodes: losing one is a full outage")


def drain(
    settings: Settings, *, node_name: str | None, timeout: str, min_availability: float
) -> Report | None:
    """Graceful node maintenance: cordon + drain (respects PDBs), then uncordon."""
    node = _pick_node(settings, node_name)
    _check_spare_capacity(settings)
    console.confirm(f"Drain node {node.name}?")
    with Monitor(settings, f"chaos drain {node.name}", min_availability=min_availability) as m:
        try:
            m.event(f"draining {node.name}")
            run(
                kube.kubectl(
                    settings,
                    "drain",
                    node.name,
                    "--ignore-daemonsets",
                    "--delete-emptydir-data",
                    f"--timeout={timeout}",
                    namespaced=False,
                )
            )
            m.event(f"{node.name} drained")
            # Replicas that no longer fit the spread constraints stay Pending until the uncordon
            kube.wait_app_ready(settings, min_one_replica=True)
            m.event("every workload has a ready replica on the remaining node")
        finally:
            run(kube.kubectl(settings, "uncordon", node.name, namespaced=False))
            m.event(f"{node.name} uncordoned")
    console.info("pods are not moved back automatically; they spread again on the next rollout")
    return m.report


def kill_node(
    settings: Settings, *, node_name: str | None, down_time: float, min_availability: float
) -> Report | None:
    """Hard node failure: power off the node's VM (deallocate), then start it again."""
    azure.ensure_login(settings)
    node = _pick_node(settings, node_name)
    instance = kube.vmss_instance(node)
    _check_spare_capacity(settings)
    console.warn(
        f"this powers off {node.name} (VMSS {instance.vmss} instance {instance.instance_id}) "
        f"for about {console.format_duration(down_time)}"
    )
    console.confirm(f"Kill node {node.name}?")
    vmss_args = [
        "--resource-group",
        instance.resource_group,
        "--name",
        instance.vmss,
        "--instance-ids",
        instance.instance_id,
    ]
    with Monitor(settings, f"chaos node-kill {node.name}", min_availability=min_availability) as m:
        # Set before the call: a Ctrl-C during the deallocation must still start the VM.
        must_restart = False
        try:
            m.event(f"deallocating {node.name}")
            must_restart = True
            run(azure.az(settings, "vmss", "deallocate", *vmss_args))
            m.event(f"{node.name} VM deallocated")

            def node_down() -> tuple[bool, str]:
                current = next(
                    (n for n in kube.nodes(settings, echo=False) if n.name == node.name), None
                )
                ready = current is not None and current.ready
                return not ready, f"{node.name} {'Ready' if ready else 'NotReady'}"

            kube.wait_until(f"{node.name} NotReady", node_down, timeout=180, interval=5)
            m.event(f"{node.name} NotReady")
            console.info(
                f"keeping the node down for {console.format_duration(down_time)} "
                "(pods are evicted from a NotReady node after 5 min)"
            )
            m.hold(down_time)
        finally:
            if must_restart:
                run(azure.az(settings, "vmss", "start", *vmss_args))
                m.event(f"{node.name} VM started")
                kube.wait_nodes_ready(settings)
                m.event(f"{node.name} Ready")
                kube.wait_app_ready(settings)
                m.event("all workloads ready")
    return m.report


def bad_release(
    settings: Settings,
    *,
    mode: BadReleaseMode,
    component: str,
    timeout: str,
    min_availability: float,
) -> Report | None:
    """Deploy a release that never gets ready and check Helm rolls it back by itself."""
    before = helm.current_revision(settings)
    if before is None:
        raise BernsteinError(f"release {settings.helm_release} is not deployed; deploy first")
    tag = helm.deployed_tag(settings)
    if mode is BadReleaseMode.PROBE:
        broken = f"{component}.probe.readiness.httpGet.path={BROKEN_READINESS_PATH}"
        console.info(f"breaking {component}: readiness probe on {BROKEN_READINESS_PATH} (404)")
    else:
        broken = f"{component}.image.tag=does-not-exist-{int(time.time())}"
        console.info(f"breaking {component}: image tag that does not exist (ImagePullBackOff)")
    console.info(f"current revision {before.revision}, image tag {tag}")
    name = f"chaos bad-release {mode.value} {component}"
    with Monitor(settings, name, min_availability=min_availability) as monitor:
        try:
            # --reuse-values: the broken release differs from the current one by one value only.
            run(
                helm.helm(
                    settings,
                    "upgrade",
                    settings.helm_release,
                    shown_path(CHART_DIR),
                    "--reuse-values",
                    "--set",
                    broken,
                    *helm.rollback_on_failure_flags(),
                    "--timeout",
                    timeout,
                )
            )
        except CommandFailedError:
            monitor.event("helm upgrade failed and rolled back, as expected")
        else:
            if not state.dry_run:
                monitor.event("the broken release was accepted: rolling back by hand")
                helm.rollback(settings, before.revision)
                raise BernsteinError(
                    f"the broken release became ready: does {component} have a readiness probe?"
                )
        kube.wait_app_ready(settings)
    if not state.dry_run:
        after = helm.current_revision(settings)
        restored_tag = helm.deployed_tag(settings)
        if after is None or restored_tag != tag:
            raise BernsteinError(f"release not restored (revision {after}, tag {restored_tag})")
        console.success(
            f"rolled back: revision {after.revision} ({after.description}), image tag {tag}",
            action=True,
        )
    return monitor.report
