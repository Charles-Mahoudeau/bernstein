"""Command-line interface: ``bernstein <group> <command> [options]``."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from . import azure, chaos, console, helm, images, kube, scenarios, vote
from .config import DEFAULT_ENV_FILE, Settings
from .errors import BernsteinError
from .probe import Monitor
from .runner import state as runner_state

app = typer.Typer(
    name="bernstein",
    help="Deploy the Bernstein poll app on AKS, roll releases and prove it stays available.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
registry_app = typer.Typer(help="Azure Container Registry (ACR).", no_args_is_help=True)
cluster_app = typer.Typer(help="AKS cluster.", no_args_is_help=True)
traefik_app = typer.Typer(help="Traefik ingress controller.", no_args_is_help=True)
images_app = typer.Typer(help="Application images.", no_args_is_help=True)
chaos_app = typer.Typer(
    help="Disruption tests under the availability monitor.", no_args_is_help=True
)
app.add_typer(registry_app, name="registry")
app.add_typer(cluster_app, name="cluster")
app.add_typer(traefik_app, name="traefik")
app.add_typer(images_app, name="images")
app.add_typer(chaos_app, name="chaos")

# --- shared options ------------------------------------------------------------

MinAvailability = Annotated[
    float,
    typer.Option(
        "--min-availability",
        min=0.0,
        max=100.0,
        help="Fail (exit code 3) if any probed URL is below this percentage of successes.",
    ),
]
Tag = Annotated[
    str | None,
    typer.Option("--tag", "-t", help="Image tag. Default: IMAGE_TAG, then the git commit."),
]
Remote = Annotated[
    bool, typer.Option("--remote", help="Build in Azure with 'az acr build' (no local Docker).")
]
Sets = Annotated[
    list[str] | None,
    typer.Option("--set", help="Extra Helm value, e.g. --set poll.replicas=3 (repeatable)."),
]
HelmTimeout = Annotated[
    str, typer.Option("--timeout", help="Helm wait timeout before rolling back, e.g. 3m.")
]
NodeName = Annotated[
    str | None,
    typer.Option("--node", help="Node to target. Default: the one with the most app pods."),
]


def _settings(ctx: typer.Context) -> Settings:
    settings = ctx.obj
    assert isinstance(settings, Settings)
    return settings


@app.callback()
def _global(
    ctx: typer.Context,
    env_file: Annotated[
        Path,
        typer.Option(
            "--env-file", help="Settings file. Default: .env at the repo root.", show_default=False
        ),
    ] = DEFAULT_ENV_FILE,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Print the commands that change things, run none."),
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Hide read-only queries and details.")
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Also print the output of queries.")
    ] = False,
    color: Annotated[
        console.ColorMode,
        typer.Option("--color", help="Colors: auto (terminal and no NO_COLOR), always, never."),
    ] = console.ColorMode.AUTO,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Answer yes to every confirmation.")
    ] = False,
) -> None:
    console.configure(color, quiet=quiet, verbose=verbose, assume_yes=yes, dry_run=dry_run)
    runner_state.dry_run = dry_run
    if dry_run:
        console.warn("dry run: commands that change something are printed, not executed")
    ctx.obj = Settings.load(env_file)


# --- top level -----------------------------------------------------------------


@app.command()
def preflight(ctx: typer.Context) -> None:
    """Check tools, Azure login, VM size restrictions and vCPU quota."""
    azure.preflight(_settings(ctx))


@app.command()
def up(ctx: typer.Context, remote: Remote = False, tag: Tag = None) -> None:
    """Create everything from scratch and deploy (registry, cluster, Traefik, images, app)."""
    scenarios.up(_settings(ctx), remote=remote, tag=tag)


@app.command()
def down(
    ctx: typer.Context,
    everything: Annotated[
        bool, typer.Option("--all", help="Also delete the registry and its images.")
    ] = False,
) -> None:
    """Delete the cluster (and the registry with --all). The resource group is kept."""
    scenarios.down(_settings(ctx), everything=everything)


@app.command()
def deploy(
    ctx: typer.Context,
    tag: Tag = None,
    sets: Sets = None,
    timeout: HelmTimeout = "3m",
) -> None:
    """Install or upgrade the app (Secrets, Traefik if missing, Helm with auto-rollback)."""
    settings = _settings(ctx)
    helm.deploy(
        settings, tag=images.resolve_tag(settings, tag), extra_sets=sets or [], timeout=timeout
    )


@app.command()
def release(
    ctx: typer.Context,
    tag: Tag = None,
    remote: Remote = False,
    force: Annotated[
        bool, typer.Option("--force", help="Rebuild even if the tag is already in the registry.")
    ] = False,
    same_images: Annotated[
        bool,
        typer.Option(
            "--same-images",
            help="Copy the deployed images to a new tag instead of building: a rolling update "
            "with the same code.",
        ),
    ] = False,
    sets: Sets = None,
    timeout: HelmTimeout = "5m",
    min_availability: MinAvailability = 100.0,
    skip_vote: Annotated[
        bool, typer.Option("--skip-vote", help="Do not run the vote check afterwards.")
    ] = False,
) -> None:
    """Rolling update: build and push, then upgrade while probing availability."""
    scenarios.release(
        _settings(ctx),
        tag=tag,
        remote=remote,
        force=force,
        same_images=same_images,
        extra_sets=sets or [],
        timeout=timeout,
        min_availability=min_availability,
        check_vote=not skip_vote,
    )


@app.command()
def rollback(
    ctx: typer.Context,
    revision: Annotated[
        int | None, typer.Argument(help="Revision to return to. Default: the previous one.")
    ] = None,
    min_availability: MinAvailability = 100.0,
) -> None:
    """Roll the release back while probing availability (see 'history')."""
    scenarios.rollback(_settings(ctx), revision=revision, min_availability=min_availability)


@app.command()
def history(ctx: typer.Context) -> None:
    """Show the Helm revisions of the release."""
    helm.print_history(_settings(ctx))


@app.command()
def status(ctx: typer.Context) -> None:
    """Show the release, workloads, pods and URLs."""
    helm.print_status(_settings(ctx))


@app.command()
def uninstall(
    ctx: typer.Context,
    delete_namespace: Annotated[
        bool,
        typer.Option("--delete-namespace", help="Also delete the namespace, Secrets and volumes."),
    ] = False,
) -> None:
    """Remove the app release from the cluster."""
    helm.uninstall(_settings(ctx), delete_namespace=delete_namespace)


@app.command()
def probe(
    ctx: typer.Context,
    duration: Annotated[
        float | None,
        typer.Option("--duration", "-d", help="Seconds to probe. Default: until Ctrl-C."),
    ] = None,
    rate: Annotated[
        float | None, typer.Option("--rate", help="Requests per second per URL (PROBE_RATE).")
    ] = None,
    min_availability: MinAvailability = 100.0,
) -> None:
    """Probe the app's URLs and report availability (run it beside a manual operation)."""
    settings = _settings(ctx)
    with Monitor(settings, "probe", min_availability=min_availability, rate=rate) as monitor:
        monitor.hold(duration)


@app.command("vote-check")
def vote_check(
    ctx: typer.Context,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds to wait for the vote.")] = 30,
) -> None:
    """Cast a vote on Poll and wait for it in Result's live tally (end-to-end check)."""
    vote.vote_check(_settings(ctx), timeout=timeout)


@app.command()
def demo(
    ctx: typer.Context,
    with_node_kill: Annotated[
        bool, typer.Option("--with-node-kill", help="Also power off a node (slow).")
    ] = False,
    min_availability: MinAvailability = 100.0,
) -> None:
    """Run the evaluation scenario: rolling update, rollback, bad release, pod and node chaos."""
    scenarios.demo(_settings(ctx), with_node_kill=with_node_kill, min_availability=min_availability)


# --- registry --------------------------------------------------------------------


@registry_app.command("create")
def registry_create(ctx: typer.Context) -> None:
    """Create the registry; generates AZURE_ACR_NAME into .env if empty."""
    azure.registry_create(_settings(ctx))


@registry_app.command("delete")
def registry_delete(ctx: typer.Context) -> None:
    """Delete the registry and all its images."""
    azure.registry_delete(_settings(ctx))


@registry_app.command("login")
def registry_login(ctx: typer.Context) -> None:
    """Log Docker in to the registry."""
    azure.registry_login(_settings(ctx))


@registry_app.command("show")
def registry_show(ctx: typer.Context) -> None:
    """Show the registry and its image tags."""
    azure.registry_show(_settings(ctx))


# --- cluster ---------------------------------------------------------------------


@cluster_app.command("create")
def cluster_create(ctx: typer.Context) -> None:
    """Create the cluster (or fix an existing one), attach the ACR, fetch credentials."""
    azure.cluster_create(_settings(ctx))


@cluster_app.command("delete")
def cluster_delete(ctx: typer.Context) -> None:
    """Delete the cluster. The registry is kept."""
    azure.cluster_delete(_settings(ctx))


@cluster_app.command("start")
def cluster_start(ctx: typer.Context) -> None:
    """Start a stopped cluster and wait for its nodes."""
    azure.cluster_start(_settings(ctx))


@cluster_app.command("scale")
def cluster_scale(
    ctx: typer.Context,
    count: Annotated[int, typer.Argument(min=1, help="Number of nodes.")],
) -> None:
    """Change the number of nodes (mind the vCPU quota, see 'preflight')."""
    azure.cluster_scale(_settings(ctx), count)


@cluster_app.command("stop")
def cluster_stop(ctx: typer.Context) -> None:
    """Stop the cluster: VMs are no longer billed, state is kept."""
    azure.cluster_stop(_settings(ctx))


@cluster_app.command("status")
def cluster_status(ctx: typer.Context) -> None:
    """Show power state, node pools, nodes, and billed IPs and disks."""
    azure.cluster_status(_settings(ctx))


@cluster_app.command("credentials")
def cluster_credentials(ctx: typer.Context) -> None:
    """Write the cluster's kubectl context to ~/.kube/config."""
    settings = _settings(ctx)
    azure.ensure_login(settings)
    azure.credentials(settings)
    kube.nodes(settings)


@cluster_app.command("grant")
def cluster_grant(
    ctx: typer.Context,
    assignee: Annotated[str, typer.Argument(help="Teammate's e-mail or object ID.")],
) -> None:
    """Give a teammate cluster access (AKS Cluster User) and push rights (AcrPush)."""
    azure.grant(_settings(ctx), assignee)


# --- traefik ---------------------------------------------------------------------


@traefik_app.command("install")
def traefik_install(ctx: typer.Context) -> None:
    """Install or upgrade Traefik and print its public IP."""
    ip = helm.traefik_install(_settings(ctx))
    console.success(f"Traefik public IP: {ip}")


@traefik_app.command("uninstall")
def traefik_uninstall(ctx: typer.Context) -> None:
    """Uninstall Traefik (releases its public IP)."""
    helm.traefik_uninstall(_settings(ctx))


@traefik_app.command("ip")
def traefik_ip(ctx: typer.Context) -> None:
    """Print Traefik's public IP."""
    console.success(f"Traefik public IP: {helm.traefik_ip(_settings(ctx))}")


# --- images ----------------------------------------------------------------------


@images_app.command("build")
def images_build(
    ctx: typer.Context,
    tag: Tag = None,
    component: Annotated[
        list[str] | None,
        typer.Option("--component", "-c", help="Only this component (repeatable)."),
    ] = None,
    remote: Remote = False,
    no_push: Annotated[
        bool, typer.Option("--no-push", help="Build only (local: load into Docker).")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Rebuild even if the tag is already in the registry.")
    ] = False,
) -> None:
    """Build the images and push them to the registry."""
    settings = _settings(ctx)
    images.build(
        settings,
        tag=images.resolve_tag(settings, tag),
        names=component or [],
        remote=remote,
        push=not no_push,
        force=force,
    )


@images_app.command("list")
def images_list() -> None:
    """List the components that have a Dockerfile."""
    for name in images.components():
        console.info(name)


# --- chaos -----------------------------------------------------------------------


@chaos_app.command("pod")
def chaos_pod(
    ctx: typer.Context,
    component: Annotated[
        str | None,
        typer.Option("--component", "-c", help="Only pods of this component (poll, redis...)."),
    ] = None,
    every_pod: Annotated[
        bool, typer.Option("--all", help="Delete every pod of --component at once.")
    ] = False,
    count: Annotated[int, typer.Option("--count", min=1, help="Number of rounds.")] = 1,
    interval: Annotated[float, typer.Option("--interval", help="Seconds between rounds.")] = 10,
    min_availability: MinAvailability = 100.0,
) -> None:
    """Delete pods and check the app stays available."""
    chaos.delete_pods(
        _settings(ctx),
        component=component,
        every_pod=every_pod,
        count=count,
        interval=interval,
        min_availability=min_availability,
    )


@chaos_app.command("drain")
def chaos_drain(
    ctx: typer.Context,
    node: NodeName = None,
    timeout: Annotated[str, typer.Option("--timeout", help="kubectl drain timeout.")] = "300s",
    min_availability: MinAvailability = 100.0,
) -> None:
    """Drain a node (graceful maintenance), then uncordon it."""
    chaos.drain(_settings(ctx), node_name=node, timeout=timeout, min_availability=min_availability)


@chaos_app.command("node-kill")
def chaos_node_kill(
    ctx: typer.Context,
    node: NodeName = None,
    down_time: Annotated[
        float, typer.Option("--down-time", help="Seconds to keep the node powered off.")
    ] = 120,
    min_availability: MinAvailability = 100.0,
) -> None:
    """Power off a node's VM (hard failure), then start it again."""
    chaos.kill_node(
        _settings(ctx), node_name=node, down_time=down_time, min_availability=min_availability
    )


@chaos_app.command("bad-release")
def chaos_bad_release(
    ctx: typer.Context,
    mode: Annotated[
        chaos.BadReleaseMode,
        typer.Option(
            "--mode", help="probe: readiness never passes; image: the image tag does not exist."
        ),
    ] = chaos.BadReleaseMode.PROBE,
    component: Annotated[
        str, typer.Option("--component", "-c", help="Component to break.")
    ] = "poll",
    timeout: HelmTimeout = "90s",
    min_availability: MinAvailability = 100.0,
) -> None:
    """Deploy a broken release and check Helm rolls it back with no downtime."""
    chaos.bad_release(
        _settings(ctx),
        mode=mode,
        component=component,
        timeout=timeout,
        min_availability=min_availability,
    )


def main() -> None:
    try:
        app()
    except BernsteinError as exc:
        console.error(str(exc))
        sys.exit(exc.exit_code)
