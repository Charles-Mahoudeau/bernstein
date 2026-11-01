"""kubectl helpers: typed views of workloads, pods and nodes, and wait loops."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import console
from .errors import BernsteinError
from .runner import run, run_json, state

if TYPE_CHECKING:
    from collections.abc import Callable

    from .config import Settings

COMPONENT_LABEL = "app.kubernetes.io/component"
INSTANCE_LABEL = "app.kubernetes.io/instance"

_PROVIDER_ID = re.compile(
    r"^azure:///subscriptions/(?P<sub>[^/]+)/resourceGroups/(?P<rg>[^/]+)/providers/"
    r"Microsoft\.Compute/virtualMachineScaleSets/(?P<vmss>[^/]+)/virtualMachines/(?P<id>\d+)$",
    re.IGNORECASE,
)


def kubectl(
    settings: Settings, *args: str, namespace: str | None = None, namespaced: bool = True
) -> list[str]:
    """A kubectl command pinned to the cluster's context and (by default) the app namespace."""
    cmd = ["kubectl", "--context", settings.context]
    if namespaced:
        cmd += ["-n", namespace or settings.namespace]
    return [*cmd, *args]


def app_selector(settings: Settings) -> str:
    return f"{INSTANCE_LABEL}={settings.helm_release}"


@dataclass(frozen=True)
class Workload:
    kind: str
    name: str
    component: str
    desired: int
    ready: int
    updated: int
    images: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return self.ready >= self.desired and self.updated >= self.desired

    @property
    def available(self) -> bool:
        """At least one ready replica: all a cordoned two-node cluster can offer."""
        return self.ready >= 1


@dataclass(frozen=True)
class Pod:
    name: str
    component: str
    node: str
    phase: str
    ready: bool
    restarts: int
    terminating: bool


@dataclass(frozen=True)
class Node:
    name: str
    ready: bool
    unschedulable: bool
    provider_id: str


@dataclass(frozen=True)
class VmssInstance:
    resource_group: str
    vmss: str
    instance_id: str


def _items(document: Any) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        return []
    items = document.get("items", [])
    return [item for item in items if isinstance(item, dict)]


def workloads(settings: Settings, *, echo: bool = True) -> list[Workload]:
    """Deployments and StatefulSets of the Helm release."""
    args = kubectl(
        settings, "get", "deployments,statefulsets", "-l", app_selector(settings), "-o", "json"
    )
    document = get_json(args, echo=echo)
    result: list[Workload] = []
    for item in _items(document):
        meta, spec, status = item["metadata"], item.get("spec", {}), item.get("status", {})
        containers = spec.get("template", {}).get("spec", {}).get("containers", [])
        result.append(
            Workload(
                kind=item.get("kind", "?"),
                name=meta["name"],
                component=meta.get("labels", {}).get(COMPONENT_LABEL, meta["name"]),
                desired=int(spec.get("replicas", 1)),
                ready=int(status.get("readyReplicas", 0)),
                updated=int(status.get("updatedReplicas", 0)),
                images=tuple(str(c.get("image", "")) for c in containers),
            )
        )
    return sorted(result, key=lambda w: w.component)


def pods(settings: Settings, *, component: str | None = None, echo: bool = True) -> list[Pod]:
    selector = app_selector(settings)
    if component:
        selector += f",{COMPONENT_LABEL}={component}"
    args = kubectl(settings, "get", "pods", "-l", selector, "-o", "json")
    document = get_json(args, echo=echo)
    result: list[Pod] = []
    for item in _items(document):
        meta, spec, status = item["metadata"], item.get("spec", {}), item.get("status", {})
        container_statuses = status.get("containerStatuses", [])
        result.append(
            Pod(
                name=meta["name"],
                component=meta.get("labels", {}).get(COMPONENT_LABEL, "?"),
                node=spec.get("nodeName", ""),
                phase=status.get("phase", "?"),
                ready=bool(container_statuses) and all(c.get("ready") for c in container_statuses),
                restarts=sum(int(c.get("restartCount", 0)) for c in container_statuses),
                terminating="deletionTimestamp" in meta,
            )
        )
    return sorted(result, key=lambda p: (p.component, p.name))


def nodes(settings: Settings, *, echo: bool = True) -> list[Node]:
    args = kubectl(settings, "get", "nodes", "-o", "json", namespaced=False)
    document = get_json(args, echo=echo)
    result: list[Node] = []
    for item in _items(document):
        conditions = item.get("status", {}).get("conditions", [])
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
        result.append(
            Node(
                name=item["metadata"]["name"],
                ready=ready,
                unschedulable=bool(item.get("spec", {}).get("unschedulable", False)),
                provider_id=item.get("spec", {}).get("providerID", ""),
            )
        )
    return sorted(result, key=lambda n: n.name)


def vmss_instance(node: Node) -> VmssInstance:
    match = _PROVIDER_ID.match(node.provider_id)
    if match is None:
        raise BernsteinError(
            f"node {node.name} is not an AKS VMSS instance (providerID={node.provider_id!r})"
        )
    return VmssInstance(match["rg"], match["vmss"], match["id"])


def get_json(args: list[str], *, echo: bool) -> Any:
    """Run a ``kubectl get -o json`` query; failures give None.

    Polling loops pass ``echo=False`` so the same query is not printed every few seconds.
    """
    if echo:
        return run_json(args, check=False)
    result = run(args, query=True, check=False, echo=False)
    if not result.ok:
        return None
    try:
        return result.json()
    except ValueError:
        return None


def wait_until(
    description: str,
    check: Callable[[], tuple[bool, str]],
    *,
    timeout: float,
    interval: float = 3.0,
) -> None:
    """Poll ``check`` until it reports done, printing its status when it changes.

    ``check`` returns ``(done, status_text)``. In dry-run mode nothing is waited for.
    """
    if state.dry_run:
        console.detail(f"would wait: {description}")
        return
    console.info(f"waiting: {description} (timeout {console.format_duration(timeout)})")
    start = time.monotonic()
    last = ""
    while True:
        done, status = check()
        if status != last:
            console.progress(status, start=start)
            last = status
        if done:
            return
        if time.monotonic() - start > timeout:
            raise BernsteinError(f"timed out waiting for: {description} (last state: {status})")
        time.sleep(interval)


def workloads_summary(items: list[Workload]) -> str:
    if not items:
        return "no workloads found"
    return ", ".join(f"{w.component} {w.ready}/{w.desired}" for w in items)


def wait_app_ready(
    settings: Settings, *, timeout: float = 300.0, min_one_replica: bool = False
) -> None:
    """Wait for every workload to be fully ready, or with ``min_one_replica`` for one replica each.

    The second form is for a cordoned node: topology spread constraints keep the evicted
    replicas Pending until the node is back, so the full count cannot be reached.
    """

    def check() -> tuple[bool, str]:
        items = workloads(settings, echo=False)
        ok = bool(items) and all(w.available if min_one_replica else w.healthy for w in items)
        return ok, f"ready: {workloads_summary(items)}"

    what = "every workload has a ready replica" if min_one_replica else "all workloads ready"
    wait_until(what, check, timeout=timeout)


def wait_nodes_ready(
    settings: Settings, *, count: int | None = None, timeout: float = 600.0
) -> None:
    def check() -> tuple[bool, str]:
        items = nodes(settings, echo=False)
        ready = [n for n in items if n.ready]
        target = count if count is not None else len(items)
        summary = ", ".join(f"{n.name}={'Ready' if n.ready else 'NotReady'}" for n in items)
        return bool(items) and len(ready) >= target, summary or "no nodes"

    wait_until("nodes Ready", check, timeout=timeout)


def namespace_exists(settings: Settings, name: str) -> bool:
    args = kubectl(settings, "get", "namespace", name, "-o", "name", namespaced=False)
    return run(args, query=True, check=False).ok
