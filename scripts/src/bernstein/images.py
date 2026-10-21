"""Build the application images and push them to the registry.

Every directory of ``poll-application/`` with a ``Dockerfile`` is a component.
Images are tagged with the git commit (never ``latest``) so each Helm revision
points to an exact build, and a rollback restores the exact previous images.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from . import azure, console
from .config import APP_DIR, REPO_ROOT, shown_path
from .errors import BernsteinError
from .runner import require_tool, run

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .config import Settings

PLATFORM = "linux/amd64"  # AKS nodes are x86_64, even when building on an ARM laptop


def components() -> list[str]:
    return sorted(path.parent.name for path in APP_DIR.glob("*/Dockerfile"))


def select_components(names: Sequence[str]) -> list[str]:
    available = components()
    if not names:
        return available
    unknown = sorted(set(names) - set(available))
    if unknown:
        raise BernsteinError(
            f"unknown component(s): {', '.join(unknown)} (available: {', '.join(available)})"
        )
    return [name for name in available if name in names]


def resolve_tag(settings: Settings, override: str | None = None) -> str:
    """``--tag``, then IMAGE_TAG, then the short commit SHA.

    Uncommitted changes under poll-application/ get a ``-dirty-<timestamp>`` suffix,
    so a rebuilt image never reuses the tag of a different build.
    """
    if override:
        return override
    if settings.image_tag:
        return settings.image_tag
    sha = run(["git", "rev-parse", "--short", "HEAD"], query=True, cwd=REPO_ROOT).stdout.strip()
    dirty = run(
        ["git", "status", "--porcelain", "--", str(APP_DIR.relative_to(REPO_ROOT))],
        query=True,
        cwd=REPO_ROOT,
    ).stdout.strip()
    if dirty:
        tag = f"{sha}-dirty-{time.strftime('%Y%m%d%H%M%S')}"
        console.warn(f"uncommitted changes in poll-application/: using tag {tag}")
        return tag
    return sha


def image_ref(settings: Settings, component: str, tag: str) -> str:
    return f"{settings.acr_server}/{component}:{tag}"


def build(
    settings: Settings,
    *,
    tag: str,
    names: Sequence[str] = (),
    remote: bool = False,
    push: bool = True,
    force: bool = False,
) -> None:
    """Build (and push) the selected components with the given tag."""
    settings.require("azure_acr_name")
    selected = select_components(names)
    azure.ensure_login(settings)
    if not remote:
        require_tool("docker")
        if push:
            azure.registry_login(settings)
    console.info(f"tag {tag}, components: {', '.join(selected)}")
    for component in selected:
        ref = image_ref(settings, component, tag)
        console.step(f"Image {component} -> {ref}")
        if push and not force and tag in azure.registry_tags(settings, component):
            console.success(f"{ref} already in the registry, skipping (use --force to rebuild)")
            continue
        context = shown_path(APP_DIR / component)
        if remote:
            run(
                azure.az(
                    settings,
                    "acr",
                    "build",
                    "--registry",
                    settings.azure_acr_name,
                    "--image",
                    f"{component}:{tag}",
                    "--platform",
                    PLATFORM,
                    *([] if push else ["--no-push"]),
                    context,
                )
            )
        else:
            run(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    PLATFORM,
                    "--progress",
                    "plain",
                    "--tag",
                    ref,
                    "--push" if push else "--load",
                    context,
                ]
            )
        console.success(f"{ref} {'pushed' if push else 'built'}", action=True)


def retag(settings: Settings, *, source_tag: str, tag: str) -> None:
    """Copy every component image to a new tag inside the registry (no rebuild, no Docker).

    A new tag makes Helm roll every Deployment, which gives a real rolling update
    with exactly the same code.
    """
    settings.require("azure_acr_name")
    azure.ensure_login(settings)
    for component in components():
        if source_tag not in azure.registry_tags(settings, component):
            console.warn(f"{component}:{source_tag} is not in the registry, skipping")
            continue
        run(
            azure.az(
                settings,
                "acr",
                "import",
                "--name",
                settings.azure_acr_name,
                "--source",
                image_ref(settings, component, source_tag),
                "--image",
                f"{component}:{tag}",
                "--force",
            )
        )
    console.success(f"images {source_tag} copied to tag {tag}", action=True)
