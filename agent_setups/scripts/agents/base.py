"""Base contract for per-agent config generators."""

from __future__ import annotations

import argparse
from abc import ABC, abstractmethod

from gateway import GatewayContext


class AgentGenerator(ABC):
    """Generates config file(s) for one coding agent from a GatewayContext.

    Each agent registers CLI flags and returns a mapping of relative output path
    -> file contents. The CLI writes those files (or prints them).
    """

    name: str  # CLI subcommand, e.g. "claude-code"
    help: str = ""

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:  # noqa: B027
        """Register agent-specific CLI flags. Override as needed."""

    @abstractmethod
    def generate(self, ctx: GatewayContext, args: argparse.Namespace) -> dict[str, str]:
        """Return {relative_path: contents} for the files this agent needs."""

    def install_notes(self, args: argparse.Namespace) -> str:
        """Human-readable deployment guidance printed after generation."""
        return ""
