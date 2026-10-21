"""Exceptions raised by the CLI, each mapped to an exit code."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .probe import Report


class BernsteinError(Exception):
    """An expected failure: printed as a one-line error, no traceback."""

    exit_code = 1


class ConfigError(BernsteinError):
    """Missing or invalid configuration."""


class CommandFailedError(BernsteinError):
    """A subprocess exited with a non-zero status."""

    def __init__(self, display: str, returncode: int, output: str = "") -> None:
        self.display = display
        self.returncode = returncode
        self.output = output
        super().__init__(f"command failed with exit code {returncode}: {display}")


class AvailabilityError(BernsteinError):
    """The application availability fell below the required threshold."""

    exit_code = 3

    def __init__(self, message: str, report: Report | None = None) -> None:
        self.report = report
        super().__init__(message)


class AbortedError(BernsteinError):
    """The user declined a confirmation prompt."""
