"""Registry of supported agent generators.

Add a new agent by importing its generator and appending it to REGISTRY.
"""

from agents.base import AgentGenerator
from agents.claude_code import ClaudeCodeGenerator
from agents.claude_desktop import ClaudeDesktopGenerator
from agents.codex import CodexGenerator
from agents.dsh import DshGenerator

# Ordered list of available generators. First supported agent: Claude Code.
GENERATORS: list[type[AgentGenerator]] = [
    ClaudeCodeGenerator,
    ClaudeDesktopGenerator,
    CodexGenerator,
    DshGenerator,
]

REGISTRY: dict[str, type[AgentGenerator]] = {g.name: g for g in GENERATORS}
