"""Settings read from ``.env``, the environment and command-line flags.

Precedence, highest first: command-line flags, real environment variables, the
``.env`` file, then the defaults below (taken from the AKS setup that is known to
work on the "Azure for Students" subscription). Empty values count as unset.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, fields, replace
from pathlib import Path

from dotenv import dotenv_values

from .errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
CHART_DIR = REPO_ROOT / "charts" / "poll-app"
APP_DIR = REPO_ROOT / "poll-application"
REPORTS_DIR = REPO_ROOT / "reports"
TRAEFIK_VALUES = REPO_ROOT / "charts" / "traefik-values.yaml"


@dataclass(frozen=True)
class Settings:
    """Every tunable value, named after its environment variable (lowercased)."""

    env_file: Path
    azure_subscription_id: str = ""
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_resource_group: str = "Bernstein"
    azure_location: str = "spaincentral"
    azure_aks_cluster_name: str = "bernstein-aks"
    azure_aks_node_count: int = 2
    azure_aks_vm_size: str = "Standard_B2s_v2"
    azure_aks_os_disk_gb: int = 64
    azure_aks_max_surge: str = "1"
    azure_aks_nodepool: str = "system"
    azure_aks_network_policy: str = "cilium"
    azure_acr_name: str = ""
    azure_acr_sku: str = "Basic"
    namespace: str = "poll"
    helm_release: str = "poll-app"
    kube_context: str = ""
    traefik_namespace: str = "traefik"
    poll_host: str = ""
    result_host: str = ""
    image_tag: str = ""
    probe_rate: float = 5.0
    probe_timeout: float = 2.0
    postgres_password: str = ""
    postgres_user: str = ""
    postgres_db: str = ""
    redis_password: str = ""

    @classmethod
    def load(cls, env_file: Path = DEFAULT_ENV_FILE) -> Settings:
        file_values = dotenv_values(env_file) if env_file.is_file() else {}
        values: dict[str, object] = {}
        for field in fields(cls):
            if field.name == "env_file":
                continue
            key = field.name.upper()
            raw = os.environ.get(key) or file_values.get(key) or ""
            raw = raw.strip()
            if not raw:
                continue
            values[field.name] = _convert(key, raw, field.default)
        return cls(env_file=env_file, **values)  # type: ignore[arg-type]

    def require(self, *names: str) -> None:
        """Fail with the full list of missing variables, not just the first."""
        missing = [name.upper() for name in names if not getattr(self, name)]
        if missing:
            raise ConfigError(
                f"missing required setting(s): {', '.join(missing)} "
                f"(set them in {self.env_file} or the environment)"
            )

    def with_values(self, **changes: object) -> Settings:
        return replace(self, **changes)  # type: ignore[arg-type]

    @property
    def context(self) -> str:
        """kubectl/helm context; ``az aks get-credentials`` names it after the cluster."""
        return self.kube_context or self.azure_aks_cluster_name

    @property
    def acr_server(self) -> str:
        return f"{self.azure_acr_name}.azurecr.io"


def _convert(key: str, raw: str, default: object) -> object:
    try:
        if isinstance(default, bool):
            return raw.lower() in {"1", "true", "yes", "on"}
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key}={raw!r} is not a valid {type(default).__name__}") from exc
    return raw


def shown_path(path: Path) -> str:
    """``path`` relative to the current directory when inside it, for shorter commands."""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def write_env_value(env_file: Path, key: str, value: str) -> None:
    """Set ``key=value`` in the .env file, keeping every other line and comment."""
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
    lines = env_file.read_text().splitlines() if env_file.is_file() else []
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    env_file.write_text("\n".join(lines) + "\n")
