"""Registry of supported agent generators.

Add a new agent by importing its generator and appending it to REGISTRY.
"""

from agents.base import AgentGenerator
from agents.claude_code import ClaudeCodeGenerator

# Ordered list of available generators. First supported agent: Claude Code.
GENERATORS: list[type[AgentGenerator]] = [
    ClaudeCodeGenerator,
]

REGISTRY: dict[str, type[AgentGenerator]] = {g.name: g for g in GENERATORS}
