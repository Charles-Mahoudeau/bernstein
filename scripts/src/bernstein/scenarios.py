"""Multi-step workflows: rolling release, fresh-cluster bring-up, teardown and the demo."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from . import azure, chaos, console, helm, images, vote
from .config import REPORTS_DIR, shown_path
from .errors import AvailabilityError, BernsteinError
from .probe import Monitor
from .runner import state

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .config import Settings
    from .probe import Report


def release(
    settings: Settings,
    *,
    tag: str | None,
    remote: bool,
    force: bool,
    same_images: bool,
    extra_sets: Sequence[str],
    timeout: str,
    min_availability: float,
    check_vote: bool = True,
) -> Report | None:
    """Rolling update to a new image tag while the availability monitor runs."""
    before = helm.current_revision(settings)
    current_tag = helm.deployed_tag(settings) if before is not None else None
    if same_images:
        if current_tag is None:
            raise BernsteinError("--same-images needs a deployed release")
        new_tag = tag or f"{current_tag.split('-r')[0]}-r{time.strftime('%Y%m%d%H%M%S')}"
        console.step(f"Copying the deployed images {current_tag} to {new_tag}")
        images.retag(settings, source_tag=current_tag, tag=new_tag)
    else:
        new_tag = images.resolve_tag(settings, tag)
        console.step(f"Building images {new_tag}")
        images.build(settings, tag=new_tag, remote=remote, force=force)
    if before is None:
        console.warn("no release deployed yet: first install, nothing to probe during it")
        helm.deploy(settings, tag=new_tag, extra_sets=extra_sets, timeout=timeout)
        if check_vote:
            vote.vote_check(settings)
        return None
    if new_tag == current_tag and not extra_sets:
        console.warn(f"tag {new_tag} is already deployed: no pod will be replaced")
    else:
        console.info(f"revision {before.revision} ({current_tag}) -> {new_tag}")
    _warn_host_change(settings)
    with Monitor(settings, f"release {new_tag}", min_availability=min_availability) as monitor:
        helm.deploy(settings, tag=new_tag, extra_sets=extra_sets, timeout=timeout)
    if check_vote:
        vote.vote_check(settings)
    return monitor.report


def _warn_host_change(settings: Settings) -> None:
    """The probe keeps the URLs it started with; new ingress hosts make them fail."""
    planned = helm.hosts(settings, helm.traefik_ip(settings))
    for component, url in helm.app_urls(settings).items():
        if component in planned and url != f"http://{planned[component]}":
            console.warn(
                f"the {component} ingress moves from {url} to http://{planned[component]}: "
                "the probe of the old URL may fail during this release"
            )


def rollback(settings: Settings, *, revision: int | None, min_availability: float) -> Report | None:
    label = f"rollback to {revision}" if revision is not None else "rollback to previous"
    with Monitor(settings, label, min_availability=min_availability) as monitor:
        helm.rollback(settings, revision)
    return monitor.report


def up(settings: Settings, *, remote: bool, tag: str | None) -> None:
    """From nothing to a working app: registry, cluster, Traefik, images, deploy."""
    start = time.monotonic()
    console.step("1/7 Pre-flight checks")
    azure.preflight(settings)
    console.step("2/7 Registry")
    settings = azure.registry_create(settings)
    console.step("3/7 Cluster")
    azure.cluster_create(settings)
    console.step("4/7 Traefik")
    helm.traefik_install(settings)
    resolved = images.resolve_tag(settings, tag)
    console.step(f"5/7 Images {resolved}")
    images.build(settings, tag=resolved, remote=remote)
    console.step("6/7 Deploy")
    helm.deploy(settings, tag=resolved)
    console.step("7/7 Verify")
    vote.vote_check(settings)
    console.success(f"up in {console.format_duration(time.monotonic() - start)}", action=True)


def down(settings: Settings, *, everything: bool) -> None:
    """Delete the cluster (and with ``everything`` the registry). The RG is kept."""
    azure.cluster_delete(settings)
    if everything:
        azure.registry_delete(settings)
    console.info(f"resource group {settings.azure_resource_group} is kept")


@dataclass
class DemoStep:
    name: str
    action: Callable[[], Report | None]
    outcome: str = "not run"
    report: Report | None = None
    detail: str = ""


def demo(settings: Settings, *, with_node_kill: bool, min_availability: float) -> None:
    """The evaluation scenario, one step after the other, with one combined report."""
    console.warn("the demo replaces pods, drains a node and rolls releases on the cluster")
    if with_node_kill:
        console.warn("it also powers off a node for a few minutes")
    console.confirm("Run the demo?")
    console.state.assume_yes = True  # every step was just confirmed at once

    def no_report(action: Callable[[], None]) -> Callable[[], Report | None]:
        def run_step() -> Report | None:
            action()
            return None

        return run_step

    steps = [
        DemoStep("vote check (before)", no_report(lambda: vote.vote_check(settings))),
        DemoStep(
            "rolling update",
            lambda: release(
                settings,
                tag=None,
                remote=False,
                force=False,
                same_images=True,
                extra_sets=(),
                timeout="5m",
                min_availability=min_availability,
                check_vote=False,
            ),
        ),
        DemoStep(
            "rollback",
            lambda: rollback(settings, revision=None, min_availability=min_availability),
        ),
        DemoStep(
            "bad release (readiness)",
            lambda: chaos.bad_release(
                settings,
                mode=chaos.BadReleaseMode.PROBE,
                component="poll",
                timeout="90s",
                min_availability=min_availability,
            ),
        ),
        DemoStep(
            "delete pods",
            lambda: chaos.delete_pods(
                settings,
                component=None,
                every_pod=False,
                count=3,
                interval=10,
                min_availability=min_availability,
            ),
        ),
        DemoStep(
            "drain node",
            lambda: chaos.drain(
                settings, node_name=None, timeout="300s", min_availability=min_availability
            ),
        ),
    ]
    if with_node_kill:
        steps.append(
            DemoStep(
                "kill node",
                lambda: chaos.kill_node(
                    settings, node_name=None, down_time=120, min_availability=min_availability
                ),
            )
        )
    steps.append(DemoStep("vote check (after)", no_report(lambda: vote.vote_check(settings))))

    for index, step in enumerate(steps, 1):
        console.step(f"Demo {index}/{len(steps)}: {step.name}")
        try:
            step.report = step.action()
            step.outcome = "PASS"
        except AvailabilityError as exc:
            step.outcome, step.detail = "FAIL", str(exc)
            step.report = exc.report
        except BernsteinError as exc:
            step.outcome, step.detail = "ERROR", str(exc)
            console.error(f"{step.name}: {exc}")
    _demo_summary(steps)


def _demo_summary(steps: list[DemoStep]) -> None:
    console.step("Demo summary")
    rows = [
        [
            s.name,
            s.outcome,
            f"{s.report.availability:.2f}%" if s.report else "-",
            s.report.files[0] if s.report and s.report.files else "-",
        ]
        for s in steps
    ]
    console.table("Steps", ["step", "result", "availability", "report"], rows)
    if not state.dry_run:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORTS_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-demo.json"
        document = [
            {
                "step": s.name,
                "outcome": s.outcome,
                "detail": s.detail,
                "availability": round(s.report.availability, 3) if s.report else None,
                "report": s.report.files[0] if s.report and s.report.files else None,
            }
            for s in steps
        ]
        path.write_text(json.dumps(document, indent=2) + "\n")
        console.detail(f"demo summary saved: {shown_path(path)}")
    failed = [s.name for s in steps if s.outcome != "PASS"]
    if any(s.outcome == "ERROR" for s in steps):
        raise BernsteinError(f"demo steps with errors: {', '.join(failed)}")
    if failed:
        raise AvailabilityError(f"demo steps below the availability threshold: {', '.join(failed)}")
    console.success("every demo step passed")
