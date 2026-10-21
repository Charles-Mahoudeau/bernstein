"""Azure resources: pre-flight checks, the container registry (ACR) and the AKS cluster.

The resource group is created when missing but never deleted: it may hold other
resources and, on the student subscription, it existed before this project.
"""

from __future__ import annotations

import secrets as random
from typing import TYPE_CHECKING, Any

from . import console, kube
from .config import write_env_value
from .errors import BernsteinError
from .runner import require_tool, run, run_json, state

if TYPE_CHECKING:
    from .config import Settings

# Sizes and regions known to work on the "Azure for Students" subscription when the
# configured ones are restricted (see docs/scripts.md, "Troubleshooting").
FALLBACK_VM_SIZES = ("Standard_B2as_v2", "Standard_B2ls_v2", "Standard_B2s_v2")
FALLBACK_LOCATIONS = ("spaincentral", "denmarkeast", "austriaeast")

AKS_USER_ROLE = "Azure Kubernetes Service Cluster User Role"


def az(settings: Settings, *args: str) -> list[str]:
    """An ``az`` command pinned to the configured subscription, if any."""
    cmd = ["az", *args]
    if settings.azure_subscription_id:
        cmd += ["--subscription", settings.azure_subscription_id]
    return cmd


def _tsv(settings: Settings, *args: str, check: bool = True) -> str:
    return run(az(settings, *args, "-o", "tsv"), query=True, check=check).stdout.strip()


# --- account ----------------------------------------------------------------

# Non-empty once the login was verified, so it is checked once per run.
_login_checked: list[bool] = []


def ensure_login(settings: Settings) -> None:
    """Make sure ``az`` is logged in, using the service principal from .env in CI."""
    if _login_checked:
        return
    require_tool("az")
    if run(["az", "account", "show", "-o", "none"], query=True, check=False).ok:
        _login_checked.append(True)
        return
    if settings.azure_client_id and settings.azure_client_secret and settings.azure_tenant_id:
        console.info("not logged in: using the service principal from the environment")
        run(
            [
                "az",
                "login",
                "--service-principal",
                "--username",
                settings.azure_client_id,
                "--password",
                settings.azure_client_secret,
                "--tenant",
                settings.azure_tenant_id,
                "-o",
                "none",
            ],
            secrets=[settings.azure_client_secret],
        )
        return
    tenant = f" --tenant {settings.azure_tenant_id}" if settings.azure_tenant_id else ""
    raise BernsteinError(f"the Azure CLI is not logged in; run: az login{tenant}")


def ensure_resource_group(settings: Settings) -> None:
    settings.require("azure_resource_group")
    exists = _tsv(settings, "group", "exists", "--name", settings.azure_resource_group)
    if exists == "true":
        console.detail(f"resource group {settings.azure_resource_group} exists")
        return
    run(
        az(
            settings,
            "group",
            "create",
            "--name",
            settings.azure_resource_group,
            "--location",
            settings.azure_location,
            "-o",
            "none",
        )
    )


def ensure_provider(settings: Settings, namespace: str) -> None:
    status = _tsv(
        settings, "provider", "show", "-n", namespace, "--query", "registrationState", check=False
    )
    if status == "Registered":
        console.detail(f"provider {namespace} registered")
        return
    run(az(settings, "provider", "register", "-n", namespace, "--wait"))


# --- pre-flight --------------------------------------------------------------


def preflight(settings: Settings) -> None:
    """Check tools, login, VM size availability and vCPU quota before creating anything."""
    problems: list[str] = []

    console.step("Tools")
    for tool, version_cmd in (
        ("az", ["az", "version", "--query", '"azure-cli"', "-o", "tsv"]),
        ("kubectl", ["kubectl", "version", "--client", "-o", "yaml"]),
        ("helm", ["helm", "version", "--short"]),
        ("docker", ["docker", "version", "--format", "{{.Client.Version}}"]),
    ):
        try:
            result = run(version_cmd, query=True, check=False)
        except BernsteinError:
            message = f"{tool} not found"
            if tool == "docker":
                console.warn(f"{message}: only remote builds (images build --remote) will work")
            else:
                problems.append(message)
                console.error(message)
            continue
        version = _first_version_line(result.stdout)
        console.success(f"{tool} {version}")

    console.step("Azure account")
    ensure_login(settings)
    account = run_json(az(settings, "account", "show", "-o", "json"))
    console.success(
        f"subscription {account['name']} ({account['id']}), user {account['user']['name']}"
    )
    settings.require("azure_resource_group")
    exists = _tsv(settings, "group", "exists", "--name", settings.azure_resource_group)
    if exists == "true":
        console.success(f"resource group {settings.azure_resource_group} exists", action=True)
    else:
        console.warn(f"resource group {settings.azure_resource_group} missing: it will be created")

    console.step(f"VM size {settings.azure_aks_vm_size} in {settings.azure_location}")
    size = _check_vm_size(settings, problems)

    console.step("vCPU quota")
    if size is not None:
        _check_quota(settings, *size, problems)

    console.step("Registry name")
    if settings.azure_acr_name:
        _check_registry_name(settings, problems)
    else:
        console.info("AZURE_ACR_NAME is empty: 'registry create' will generate one")

    if problems:
        console.step("Fallbacks")
        console.info(
            "set AZURE_AKS_VM_SIZE to one of: "
            + ", ".join(FALLBACK_VM_SIZES)
            + "; or AZURE_LOCATION to one of: "
            + ", ".join(FALLBACK_LOCATIONS)
        )
        raise BernsteinError("pre-flight failed:\n  - " + "\n  - ".join(problems))
    console.success("pre-flight checks passed")


def _first_version_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("gitVersion:"):
            return line.split(":", 1)[1].strip()
    return lines[0] if lines else "?"


def _check_vm_size(settings: Settings, problems: list[str]) -> tuple[int, str] | None:
    """Return (vCPUs per node, quota family) of the VM size, or None if unavailable."""
    skus: list[dict[str, Any]] = (
        run_json(
            az(
                settings,
                "vm",
                "list-skus",
                "--location",
                settings.azure_location,
                "--resource-type",
                "virtualMachines",
                "--size",
                settings.azure_aks_vm_size,
                "--all",
                "-o",
                "json",
            )
        )
        or []
    )
    sku = next((s for s in skus if s.get("name") == settings.azure_aks_vm_size), None)
    if sku is None:
        problems.append(f"{settings.azure_aks_vm_size} is not offered in {settings.azure_location}")
        console.error(problems[-1])
        return None
    restrictions = sku.get("restrictions") or []
    location_restricted = [r for r in restrictions if r.get("type") == "Location"]
    zone_restricted = [r for r in restrictions if r.get("type") == "Zone"]
    if location_restricted:
        reason = location_restricted[0].get("reasonCode", "restricted")
        problems.append(f"{settings.azure_aks_vm_size} is restricted in the region ({reason})")
        console.error(problems[-1])
    elif zone_restricted:
        console.warn(
            f"{settings.azure_aks_vm_size} is restricted in some zones (fine without zones)"
        )
    else:
        console.success(f"{settings.azure_aks_vm_size} is unrestricted")
    capabilities = {c.get("name"): c.get("value") for c in sku.get("capabilities", [])}
    vcpus = int(capabilities.get("vCPUs", 0) or 0)
    family = str(sku.get("family", ""))
    console.info(f"{vcpus} vCPU per node, family {family}")
    return vcpus, family


def _check_quota(settings: Settings, vcpus: int, family: str, problems: list[str]) -> None:
    usages: list[dict[str, Any]] = (
        run_json(
            az(settings, "vm", "list-usage", "--location", settings.azure_location, "-o", "json")
        )
        or []
    )
    surge = _surge_nodes(settings)
    needed = vcpus * (settings.azure_aks_node_count + surge)
    # A running cluster already uses its nodes' vCPUs: they are not needed twice.
    cluster = _cluster(settings)
    running = cluster is not None and cluster.get("powerState", {}).get("code") == "Running"
    existing = vcpus * _pool_count(settings, cluster) if cluster is not None and running else 0
    checks = [("cores", "total regional vCPUs")]
    if family:
        checks.append((family.lower(), f"{family} vCPUs"))
    for key, label in checks:
        usage = next((u for u in usages if str(u["name"]["value"]).lower() == key), None)
        if usage is None:
            console.warn(f"no quota entry for {label}")
            continue
        limit = int(usage["limit"])
        current = max(0, int(usage["currentValue"]) - existing)
        free = limit - current
        message = (
            f"{label}: need {needed} ({settings.azure_aks_node_count} nodes + {surge} surge), "
            f"{free} free of {limit}"
        )
        if needed > free:
            problems.append(message)
            console.error(message)
        else:
            console.success(message)


def _surge_nodes(settings: Settings) -> int:
    surge = settings.azure_aks_max_surge.strip()
    if surge.endswith("%"):
        percent = int(surge[:-1] or 0)
        return max(1, -(-settings.azure_aks_node_count * percent // 100))
    return int(surge or 0)


def _check_registry_name(settings: Settings, problems: list[str]) -> None:
    if _registry(settings) is not None:
        console.success(f"registry {settings.azure_acr_name} already exists in the resource group")
        return
    check = run_json(az(settings, "acr", "check-name", "-n", settings.azure_acr_name, "-o", "json"))
    if check and check.get("nameAvailable"):
        console.success(f"{settings.azure_acr_name} is available")
    else:
        message = f"registry name {settings.azure_acr_name} is taken: {check.get('message', '')}"
        problems.append(message)
        console.error(message)


# --- registry ----------------------------------------------------------------


def _registry(settings: Settings) -> dict[str, Any] | None:
    if not settings.azure_acr_name:
        return None
    result: dict[str, Any] | None = run_json(
        az(
            settings,
            "acr",
            "show",
            "-n",
            settings.azure_acr_name,
            "-g",
            settings.azure_resource_group,
            "-o",
            "json",
        ),
        check=False,
    )
    return result


def registry_create(settings: Settings) -> Settings:
    """Create the ACR (idempotent). Returns settings updated with a generated name."""
    settings.require("azure_resource_group")
    ensure_login(settings)
    if not settings.azure_acr_name:
        name = f"bernsteinacr{random.token_hex(3)}"
        console.info(f"AZURE_ACR_NAME is empty: generated {name}")
        if state.dry_run:
            console.detail(f"would write AZURE_ACR_NAME={name} to {settings.env_file}")
        else:
            write_env_value(settings.env_file, "AZURE_ACR_NAME", name)
            console.success(f"saved AZURE_ACR_NAME={name} in {settings.env_file}")
        settings = settings.with_values(azure_acr_name=name)
    elif _registry(settings) is not None:
        console.success(f"registry {settings.acr_server} already exists")
        return settings
    ensure_provider(settings, "Microsoft.ContainerRegistry")
    ensure_resource_group(settings)
    run(
        az(
            settings,
            "acr",
            "create",
            "-g",
            settings.azure_resource_group,
            "-n",
            settings.azure_acr_name,
            "--sku",
            settings.azure_acr_sku,
            "-l",
            settings.azure_location,
            "-o",
            "none",
        )
    )
    console.success(f"registry {settings.acr_server} created")
    return settings


def registry_delete(settings: Settings) -> None:
    settings.require("azure_resource_group", "azure_acr_name")
    ensure_login(settings)
    if _registry(settings) is None:
        console.info(f"registry {settings.azure_acr_name} does not exist")
        return
    console.warn(f"this deletes {settings.acr_server} and every image in it")
    console.confirm(f"Delete registry {settings.azure_acr_name}?", expected=settings.azure_acr_name)
    run(
        az(
            settings,
            "acr",
            "delete",
            "-g",
            settings.azure_resource_group,
            "-n",
            settings.azure_acr_name,
            "--yes",
        )
    )
    console.success(
        f"registry {settings.azure_acr_name} deleted (AZURE_ACR_NAME is left in .env)", action=True
    )


def registry_login(settings: Settings) -> None:
    settings.require("azure_acr_name")
    ensure_login(settings)
    run(az(settings, "acr", "login", "-n", settings.azure_acr_name))


def registry_tags(settings: Settings, repository: str) -> list[str]:
    tags = run_json(
        az(
            settings,
            "acr",
            "repository",
            "show-tags",
            "-n",
            settings.azure_acr_name,
            "--repository",
            repository,
            "-o",
            "json",
        ),
        check=False,
    )
    return [str(tag) for tag in tags] if isinstance(tags, list) else []


def registry_show(settings: Settings) -> None:
    settings.require("azure_resource_group", "azure_acr_name")
    ensure_login(settings)
    registry = _registry(settings)
    if registry is None:
        raise BernsteinError(f"registry {settings.azure_acr_name} does not exist")
    console.success(
        f"{registry['loginServer']} ({registry['sku']['name']}, {registry['location']}, "
        f"{registry['provisioningState']})"
    )
    repositories = (
        run_json(
            az(settings, "acr", "repository", "list", "-n", settings.azure_acr_name, "-o", "json")
        )
        or []
    )
    rows = []
    for repository in repositories:
        tags = registry_tags(settings, repository)
        rows.append([repository, str(len(tags)), ", ".join(tags[-5:])])
    console.table("Repositories", ["repository", "tags", "latest tags"], rows)


# --- cluster -----------------------------------------------------------------


def _cluster(settings: Settings) -> dict[str, Any] | None:
    if not settings.azure_resource_group:
        return None
    result: dict[str, Any] | None = run_json(
        az(
            settings,
            "aks",
            "show",
            "-g",
            settings.azure_resource_group,
            "-n",
            settings.azure_aks_cluster_name,
            "-o",
            "json",
        ),
        check=False,
    )
    return result


def _require_cluster(settings: Settings) -> dict[str, Any]:
    cluster = _cluster(settings)
    if cluster is None:
        raise BernsteinError(
            f"cluster {settings.azure_aks_cluster_name} does not exist in "
            f"{settings.azure_resource_group}; run: bernstein cluster create"
        )
    return cluster


def _network_flags(settings: Settings) -> list[str]:
    flags = ["--network-plugin", "azure", "--network-plugin-mode", "overlay"]
    policy = settings.azure_aks_network_policy.lower()
    if policy == "cilium":
        flags += ["--network-dataplane", "cilium", "--network-policy", "cilium"]
    elif policy not in {"", "none"}:
        flags += ["--network-policy", policy]
    return flags


def cluster_create(settings: Settings) -> None:
    """Create the AKS cluster (idempotent), attach the ACR and fetch credentials."""
    settings.require("azure_resource_group", "azure_acr_name")
    ensure_login(settings)
    cluster = _cluster(settings)
    if cluster is None:
        ensure_resource_group(settings)
        console.step(f"Creating AKS cluster {settings.azure_aks_cluster_name} (about 5-10 min)")
        run(
            az(
                settings,
                "aks",
                "create",
                "-g",
                settings.azure_resource_group,
                "-n",
                settings.azure_aks_cluster_name,
                "-l",
                settings.azure_location,
                "--node-count",
                str(settings.azure_aks_node_count),
                "--node-vm-size",
                settings.azure_aks_vm_size,
                "--node-osdisk-size",
                str(settings.azure_aks_os_disk_gb),
                "--nodepool-name",
                settings.azure_aks_nodepool,
                "--tier",
                "free",
                "--attach-acr",
                settings.azure_acr_name,
                "--generate-ssh-keys",
                *_network_flags(settings),
                "-o",
                "none",
            )
        )
        cluster = _cluster(settings) or {}
        nodes = settings.azure_aks_node_count
    else:
        console.success(f"cluster {settings.azure_aks_cluster_name} already exists")
        if cluster.get("powerState", {}).get("code") == "Stopped":
            raise BernsteinError("the cluster is stopped; run: bernstein cluster start")
        ensure_acr_attached(settings, cluster)
        nodes = _pool_count(settings, cluster)
        if nodes != settings.azure_aks_node_count:
            console.warn(
                f"the node pool has {nodes} node(s) but AZURE_AKS_NODE_COUNT is "
                f"{settings.azure_aks_node_count}; run: bernstein cluster scale "
                f"{settings.azure_aks_node_count}"
            )
    ensure_max_surge(settings, cluster)
    credentials(settings)
    kube.wait_nodes_ready(settings, count=nodes)
    console.success(f"cluster {settings.azure_aks_cluster_name} is ready", action=True)


def _pool(settings: Settings, cluster: dict[str, Any]) -> dict[str, Any]:
    pools = cluster.get("agentPoolProfiles") or []
    pool = next((p for p in pools if p.get("name") == settings.azure_aks_nodepool), None)
    if pool is None:
        names = ", ".join(str(p.get("name")) for p in pools)
        raise BernsteinError(
            f"node pool {settings.azure_aks_nodepool} not found (pools: {names}); "
            "set AZURE_AKS_NODEPOOL"
        )
    return dict(pool)


def _pool_count(settings: Settings, cluster: dict[str, Any]) -> int:
    return int(_pool(settings, cluster).get("count") or 0)


def ensure_acr_attached(settings: Settings, cluster: dict[str, Any]) -> None:
    """Grant the kubelet identity AcrPull on the registry if it is missing."""
    kubelet = cluster.get("identityProfile", {}).get("kubeletidentity", {}).get("objectId", "")
    registry = _registry(settings)
    if not kubelet or registry is None:
        console.warn("cannot verify the ACR attachment (missing kubelet identity or registry)")
        return
    assignments = run_json(
        az(
            settings,
            "role",
            "assignment",
            "list",
            "--assignee",
            kubelet,
            "--scope",
            registry["id"],
            "--role",
            "AcrPull",
            "-o",
            "json",
        ),
        check=False,
    )
    if assignments:
        console.detail(f"{settings.azure_acr_name} is attached (kubelet has AcrPull)")
        return
    run(
        az(
            settings,
            "aks",
            "update",
            "-g",
            settings.azure_resource_group,
            "-n",
            settings.azure_aks_cluster_name,
            "--attach-acr",
            settings.azure_acr_name,
            "-o",
            "none",
        )
    )


def ensure_max_surge(settings: Settings, cluster: dict[str, Any]) -> None:
    """``--max-surge`` is not an ``az aks create`` flag, so set it on the node pool."""
    pool = _pool(settings, cluster) if cluster else {}
    current = (pool.get("upgradeSettings") or {}).get("maxSurge")
    if current == settings.azure_aks_max_surge:
        console.detail(f"node pool max surge is already {current}")
        return
    run(
        az(
            settings,
            "aks",
            "nodepool",
            "update",
            "-g",
            settings.azure_resource_group,
            "--cluster-name",
            settings.azure_aks_cluster_name,
            "-n",
            settings.azure_aks_nodepool,
            "--max-surge",
            settings.azure_aks_max_surge,
            "-o",
            "none",
        )
    )


def credentials(settings: Settings) -> None:
    settings.require("azure_resource_group")
    run(
        az(
            settings,
            "aks",
            "get-credentials",
            "-g",
            settings.azure_resource_group,
            "-n",
            settings.azure_aks_cluster_name,
            "--context",
            settings.context,
            "--overwrite-existing",
        )
    )


def cluster_delete(settings: Settings) -> None:
    settings.require("azure_resource_group")
    ensure_login(settings)
    if _cluster(settings) is None:
        console.info(f"cluster {settings.azure_aks_cluster_name} does not exist")
        return
    console.warn("this deletes the cluster, its nodes, public IPs and persistent volumes")
    console.warn("the registry and its images are kept")
    console.confirm(
        f"Delete cluster {settings.azure_aks_cluster_name}?",
        expected=settings.azure_aks_cluster_name,
    )
    run(
        az(
            settings,
            "aks",
            "delete",
            "-g",
            settings.azure_resource_group,
            "-n",
            settings.azure_aks_cluster_name,
            "--yes",
        )
    )
    console.success(f"cluster {settings.azure_aks_cluster_name} deleted", action=True)


def cluster_start(settings: Settings) -> None:
    settings.require("azure_resource_group")
    ensure_login(settings)
    cluster = _require_cluster(settings)
    if cluster.get("powerState", {}).get("code") == "Running":
        console.success("cluster is already running")
    else:
        console.step("Starting the cluster (about 3-5 min)")
        run(
            az(
                settings,
                "aks",
                "start",
                "-g",
                settings.azure_resource_group,
                "-n",
                settings.azure_aks_cluster_name,
            )
        )
    kube.wait_nodes_ready(settings, count=_pool_count(settings, cluster))


def cluster_scale(settings: Settings, count: int) -> None:
    """Change the number of nodes of the node pool."""
    settings.require("azure_resource_group")
    ensure_login(settings)
    current = _pool_count(settings, _require_cluster(settings))
    if current == count:
        console.success(
            f"node pool {settings.azure_aks_nodepool} already has {count} node(s)", action=True
        )
        return
    if count < current:
        console.warn("pods on the removed node(s) are evicted")
        console.confirm(f"Scale {settings.azure_aks_nodepool} from {current} to {count} node(s)?")
    run(
        az(
            settings,
            "aks",
            "scale",
            "-g",
            settings.azure_resource_group,
            "-n",
            settings.azure_aks_cluster_name,
            "--nodepool-name",
            settings.azure_aks_nodepool,
            "--node-count",
            str(count),
            "-o",
            "none",
        )
    )
    kube.wait_nodes_ready(settings, count=count)
    console.success(f"node pool {settings.azure_aks_nodepool} has {count} node(s)")


def cluster_stop(settings: Settings) -> None:
    settings.require("azure_resource_group")
    ensure_login(settings)
    cluster = _require_cluster(settings)
    if cluster.get("powerState", {}).get("code") == "Stopped":
        console.success("cluster is already stopped")
        return
    console.warn("stopping cuts access for everyone using the cluster (tell your teammate)")
    console.confirm(f"Stop cluster {settings.azure_aks_cluster_name}?")
    run(
        az(
            settings,
            "aks",
            "stop",
            "-g",
            settings.azure_resource_group,
            "-n",
            settings.azure_aks_cluster_name,
        )
    )
    console.success(
        "cluster stopped: VMs are no longer billed (ACR, disks and IPs still are)", action=True
    )


def cluster_status(settings: Settings) -> None:
    settings.require("azure_resource_group")
    ensure_login(settings)
    cluster = _require_cluster(settings)
    power = cluster.get("powerState", {}).get("code", "?")
    console.success(
        f"{cluster['name']}: power {power}, provisioning {cluster.get('provisioningState')}, "
        f"Kubernetes {cluster.get('currentKubernetesVersion')}, {cluster.get('location')}"
    )
    pool_rows = [
        [
            str(p.get("name")),
            str(p.get("count")),
            str(p.get("vmSize")),
            str((p.get("upgradeSettings") or {}).get("maxSurge", "-")),
            str(p.get("powerState", {}).get("code", "?")),
        ]
        for p in cluster.get("agentPoolProfiles") or []
    ]
    console.table("Node pools", ["pool", "count", "size", "max surge", "power"], pool_rows)
    if power == "Running":
        node_rows = [
            [n.name, "Ready" if n.ready else "NotReady", "yes" if n.unschedulable else "no"]
            for n in kube.nodes(settings)
        ]
        console.table("Nodes", ["node", "status", "cordoned"], node_rows)
    node_rg = str(cluster.get("nodeResourceGroup", ""))
    if node_rg:
        ips = run_json(az(settings, "network", "public-ip", "list", "-g", node_rg, "-o", "json"))
        ip_rows = [[str(ip.get("name")), str(ip.get("ipAddress", "-"))] for ip in ips or []]
        console.table(f"Public IPs (billed) in {node_rg}", ["name", "address"], ip_rows)
        disks = run_json(az(settings, "disk", "list", "-g", node_rg, "-o", "json"))
        disk_rows = [
            [str(d.get("name")), f"{d.get('diskSizeGb')} GB", str(d.get("diskState"))]
            for d in disks or []
        ]
        console.table(f"Managed disks (billed) in {node_rg}", ["name", "size", "state"], disk_rows)


def node_resource_group(settings: Settings) -> str:
    return str(_require_cluster(settings).get("nodeResourceGroup", ""))


def grant(settings: Settings, assignee: str) -> None:
    """Give a teammate kubectl access to the cluster and push access to the registry."""
    settings.require("azure_resource_group", "azure_acr_name")
    ensure_login(settings)
    aks_id = str(_require_cluster(settings)["id"])
    registry = _registry(settings)
    if registry is None:
        raise BernsteinError(f"registry {settings.azure_acr_name} does not exist")
    for role, scope in ((AKS_USER_ROLE, aks_id), ("AcrPush", str(registry["id"]))):
        run(
            az(
                settings,
                "role",
                "assignment",
                "create",
                "--assignee",
                assignee,
                "--role",
                role,
                "--scope",
                scope,
                "-o",
                "none",
            )
        )
    console.success(f"{assignee} can now run:", action=True)
    console.info(
        f"az aks get-credentials -g {settings.azure_resource_group} "
        f"-n {settings.azure_aks_cluster_name}"
    )
    console.info(f"az acr login -n {settings.azure_acr_name}")
