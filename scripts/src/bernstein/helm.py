"""Helm releases: Traefik, the poll-app chart, history and rollback."""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import console, images, kube
from .config import CHART_DIR, TRAEFIK_VALUES, shown_path
from .errors import BernsteinError
from .runner import run, run_json, state

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .config import Settings

TRAEFIK_REPO = "https://traefik.github.io/charts"
TRAEFIK_RELEASE = "traefik"
# Components whose image tag the deploy sets; the chart ignores values it does not use.
WEB_COMPONENTS = ("poll", "result")
DRY_RUN_IP = "<traefik-ip>"


def helm(settings: Settings, *args: str, namespace: str | None = None) -> list[str]:
    return [
        "helm",
        "--kube-context",
        settings.context,
        "--namespace",
        namespace or settings.namespace,
        *args,
    ]


@functools.cache
def helm_major_version() -> int:
    output = run(["helm", "version", "--short"], query=True).stdout
    match = re.search(r"v(\d+)\.", output)
    return int(match[1]) if match else 3


def rollback_on_failure_flags() -> list[str]:
    """Helm 4 renamed ``--atomic`` to ``--rollback-on-failure``."""
    if helm_major_version() >= 4:
        return ["--rollback-on-failure", "--wait=watcher"]
    return ["--atomic", "--wait"]


# --- Traefik -----------------------------------------------------------------


def traefik_installed(settings: Settings) -> bool:
    args = helm(settings, "status", TRAEFIK_RELEASE, namespace=settings.traefik_namespace)
    return run(args, query=True, check=False).ok


def traefik_install(settings: Settings) -> str:
    """Install or upgrade Traefik and return its public IP."""
    run(["helm", "repo", "add", "traefik", TRAEFIK_REPO, "--force-update"])
    run(["helm", "repo", "update", "traefik"])
    values = ["--values", shown_path(TRAEFIK_VALUES)] if TRAEFIK_VALUES.is_file() else []
    run(
        helm(
            settings,
            "upgrade",
            "--install",
            TRAEFIK_RELEASE,
            "traefik/traefik",
            "--create-namespace",
            "--wait",
            "--timeout",
            "5m",
            *values,
            namespace=settings.traefik_namespace,
        )
    )
    return traefik_ip(settings)


def traefik_uninstall(settings: Settings) -> None:
    if not traefik_installed(settings):
        console.info("Traefik is not installed")
        return
    console.warn("the public IP of Traefik is released: nip.io hostnames will change")
    console.confirm("Uninstall Traefik?")
    run(
        helm(settings, "uninstall", TRAEFIK_RELEASE, "--wait", namespace=settings.traefik_namespace)
    )


def traefik_ip(settings: Settings, *, timeout: float = 300.0) -> str:
    """Wait for the LoadBalancer IP of the Traefik Service."""
    args = kube.kubectl(
        settings,
        "get",
        "service",
        TRAEFIK_RELEASE,
        "-o",
        "json",
        namespace=settings.traefik_namespace,
    )
    found: list[str] = []

    def check(*, echo: bool = False) -> tuple[bool, str]:
        service = kube.get_json(args, echo=echo) or {}
        ingress = service.get("status", {}).get("loadBalancer", {}).get("ingress") or []
        ip = next((str(i["ip"]) for i in ingress if i.get("ip")), "")
        if ip:
            found.append(ip)
            return True, f"Traefik public IP: {ip}"
        return False, "Traefik public IP pending"

    if check(echo=True)[0]:
        return found[0]
    if state.dry_run:
        return DRY_RUN_IP
    kube.wait_until("Traefik LoadBalancer IP", check, timeout=timeout, interval=5)
    return found[0]


# --- poll-app ----------------------------------------------------------------


@dataclass(frozen=True)
class Revision:
    revision: int
    status: str
    chart: str
    app_version: str
    description: str
    updated: str


def ensure_namespace(settings: Settings) -> None:
    if kube.namespace_exists(settings, settings.namespace):
        return
    run(kube.kubectl(settings, "create", "namespace", settings.namespace, namespaced=False))


def apply_secret(settings: Settings, name: str, key: str, value: str) -> None:
    """Create or update a Secret; the value goes through stdin, never on the command line."""
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": settings.namespace,
            "labels": {"app.kubernetes.io/managed-by": "bernstein"},
        },
        "type": "Opaque",
        "stringData": {key: value},
    }
    console.detail(f"Secret {name} with key {key} (from {key} in .env), sent on stdin:")
    run(
        kube.kubectl(settings, "apply", "-f", "-"),
        stdin=json.dumps(manifest),
        secrets=[value],
    )


def apply_secrets(settings: Settings) -> None:
    settings.require("postgres_password")
    apply_secret(settings, "postgres-secret", "POSTGRES_PASSWORD", settings.postgres_password)
    if settings.redis_password:
        apply_secret(settings, "redis-secret", "REDIS_PASSWORD", settings.redis_password)
    else:
        # Poll reads this Secret as optional: a stale one would make it send a password
        # to a Redis that has none, which Redis rejects.
        run(kube.kubectl(settings, "delete", "secret", "redis-secret", "--ignore-not-found"))


def hosts(settings: Settings, ip: str) -> dict[str, str]:
    """Ingress hostnames: POLL_HOST/RESULT_HOST, or nip.io names that resolve to the IP."""
    return {
        "poll": settings.poll_host or f"poll.{ip}.nip.io",
        "result": settings.result_host or f"result.{ip}.nip.io",
    }


def deploy(
    settings: Settings,
    *,
    tag: str,
    extra_sets: Sequence[str] = (),
    timeout: str = "3m",
    prepare: bool = True,
) -> None:
    """``helm upgrade --install`` with automatic rollback if the release never gets ready.

    ``prepare`` creates the namespace and Secrets and installs Traefik if needed;
    the chaos tests skip it since they only change the release.
    """
    settings.require("azure_acr_name")
    if prepare:
        ensure_namespace(settings)
        apply_secrets(settings)
        if not traefik_installed(settings):
            console.step("Installing Traefik")
            traefik_install(settings)
    ip = traefik_ip(settings)
    names = hosts(settings, ip)
    sets = ["--set", f"registry.url={settings.acr_server}"]
    for component in images.components():
        sets += ["--set-string", f"{component}.image.tag={tag}"]
    for component in WEB_COMPONENTS:
        sets += [
            "--set",
            f"{component}.ingress.enabled=true",
            "--set-string",
            f"{component}.ingress.host={names[component]}",
        ]
    if settings.redis_password:
        sets += ["--set", "redis.auth.enabled=true"]
    for item in extra_sets:
        sets += ["--set", item]
    run(
        helm(
            settings,
            "upgrade",
            "--install",
            settings.helm_release,
            shown_path(CHART_DIR),
            *sets,
            *rollback_on_failure_flags(),
            "--timeout",
            timeout,
        )
    )
    current = current_revision(settings)
    if current is not None and not state.dry_run:
        console.success(f"release {settings.helm_release} revision {current.revision} deployed")
    print_urls(settings)


def history(settings: Settings) -> list[Revision]:
    document = run_json(
        helm(settings, "history", settings.helm_release, "-o", "json"),
        check=False,
    )
    if not isinstance(document, list):
        return []
    return [
        Revision(
            revision=int(item.get("revision", 0)),
            status=str(item.get("status", "")),
            chart=str(item.get("chart", "")),
            app_version=str(item.get("app_version", "")),
            description=str(item.get("description", "")),
            updated=str(item.get("updated", ""))[:19].replace("T", " "),
        )
        for item in document
    ]


def current_revision(settings: Settings) -> Revision | None:
    deployed = [r for r in history(settings) if r.status == "deployed"]
    return deployed[-1] if deployed else None


def print_history(settings: Settings) -> None:
    rows = [
        [str(r.revision), r.updated, r.status, r.chart, r.description] for r in history(settings)
    ]
    if not rows:
        raise BernsteinError(f"release {settings.helm_release} not found in {settings.namespace}")
    console.table(
        f"History of {settings.helm_release}",
        ["rev", "updated", "status", "chart", "description"],
        rows,
    )


def deployed_values(settings: Settings) -> dict[str, Any]:
    values = run_json(
        helm(settings, "get", "values", settings.helm_release, "-o", "json"), check=False
    )
    return values if isinstance(values, dict) else {}


def deployed_tag(settings: Settings) -> str:
    """The image tag of the current release (the poll image's)."""
    tag = deployed_values(settings).get("poll", {}).get("image", {}).get("tag")
    if not tag:
        raise BernsteinError(f"release {settings.helm_release} has no poll.image.tag; deploy first")
    return str(tag)


def rollback(settings: Settings, revision: int | None = None) -> None:
    args = ["rollback", settings.helm_release]
    if revision is not None:
        args.append(str(revision))
    run(helm(settings, *args, "--wait", "--timeout", "5m"))
    current = current_revision(settings)
    if current is not None and not state.dry_run:
        console.success(f"now at revision {current.revision} ({current.description})")


def uninstall(settings: Settings, *, delete_namespace: bool = False) -> None:
    if not history(settings):
        console.info(f"release {settings.helm_release} is not installed")
    else:
        console.confirm(f"Uninstall release {settings.helm_release} from {settings.namespace}?")
        run(helm(settings, "uninstall", settings.helm_release, "--wait"))
    if delete_namespace and kube.namespace_exists(settings, settings.namespace):
        console.warn(f"deleting namespace {settings.namespace} also deletes its Secrets and PVCs")
        console.confirm(f"Delete namespace {settings.namespace}?", expected=settings.namespace)
        run(kube.kubectl(settings, "delete", "namespace", settings.namespace, namespaced=False))


def app_urls(settings: Settings, *, echo: bool = True) -> dict[str, str]:
    """``component -> URL`` from the release's Ingresses, through the Traefik IP."""
    args = kube.kubectl(settings, "get", "ingress", "-l", kube.app_selector(settings), "-o", "json")
    document = kube.get_json(args, echo=echo)
    urls: dict[str, str] = {}
    for item in (document or {}).get("items", []):
        component = item["metadata"].get("labels", {}).get(kube.COMPONENT_LABEL, "")
        rules = item.get("spec", {}).get("rules") or [{}]
        # An Ingress without a host matches any host: reach it through the bare IP.
        host = rules[0].get("host") or traefik_ip(settings, timeout=0)
        if component:
            urls[component] = f"http://{host}"
    return urls


def print_urls(settings: Settings) -> None:
    urls = app_urls(settings)
    if not urls:
        console.warn("no Ingress found for the release")
        return
    for component, url in sorted(urls.items()):
        console.info(f"{component:<8} {url}")


def print_status(settings: Settings) -> None:
    current = current_revision(settings)
    if current is None:
        console.warn(f"release {settings.helm_release} is not deployed in {settings.namespace}")
    else:
        console.success(
            f"{settings.helm_release} revision {current.revision}, {current.chart}, "
            f"deployed {current.updated}"
        )
    workload_rows = [
        [w.component, w.kind, f"{w.ready}/{w.desired}", ", ".join(w.images)]
        for w in kube.workloads(settings)
    ]
    console.table("Workloads", ["component", "kind", "ready", "image"], workload_rows)
    pod_rows = [
        [
            p.name,
            p.component,
            p.node,
            "terminating" if p.terminating else p.phase,
            "yes" if p.ready else "no",
            str(p.restarts),
        ]
        for p in kube.pods(settings)
    ]
    console.table("Pods", ["pod", "component", "node", "phase", "ready", "restarts"], pod_rows)
    console.step("URLs")
    print_urls(settings)
