"""
Parse mcp.json from skill directories.

Variables use ${config.key} to reference values from frontend skillConfigs.

Example mcp.json:

    {
      "mcpServers": {
        "sorftime": {
          "transport": "sse",
          "url": "${config.mcpUrl}",
          "headers": {
            "Authorization": "Bearer ${config.mcpKey}"
          }
        }
      }
    }

Frontend sends skillConfigs to fill in the values per user:

    {
      "skillConfigs": {
        "amazon-keywords": {
          "mcpUrl": "https://mcp.sorftime.com/sse",
          "mcpKey": "user-key-here"
        }
      }
    }
"""

import json
import logging
import re
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

SKILL_ROOT = Path(
    os.environ.get("SKILL_ROOT", "/home/hqzn/grantllama-scrape-skill/.claude/skills")
)


@dataclass
class StdioServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class SSEServerConfig:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class StreamableHTTPServerConfig:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)


MCPServerConfig = Union[StdioServerConfig, SSEServerConfig, StreamableHTTPServerConfig]


def _expand(value: str, config: dict | None = None) -> str:
    """Replace ${config.key} with values from frontend skillConfigs."""
    if not config:
        return value
    return re.sub(
        r"\$\{config\.(\w+)\}",
        lambda m: str(config.get(m.group(1), m.group(0))),
        value,
    )


def _expand_dict(d: dict[str, str], config: dict | None = None) -> dict[str, str]:
    return {k: _expand(v, config) for k, v in d.items()}


def _expand_list(lst: list[str], config: dict | None = None) -> list[str]:
    return [_expand(v, config) for v in lst]


def load_skill_mcp_config(
    skill_name: str,
    skill_config: dict | None = None,
) -> list[MCPServerConfig]:
    """Load mcp.json from a skill directory, expanding ${config.*} variables."""
    if ".." in skill_name or "/" in skill_name or "\\" in skill_name:
        return []
    config_path = SKILL_ROOT / skill_name / "mcp.json"
    if not config_path.exists():
        return []
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", config_path, exc)
        return []

    servers = raw.get("mcpServers", {})
    result = []
    for server_name, conf in servers.items():
        transport = conf.get("transport", "stdio")
        if transport == "streamable-http":
            result.append(
                StreamableHTTPServerConfig(
                    name=server_name,
                    url=_expand(conf["url"], skill_config),
                    headers=_expand_dict(conf.get("headers", {}), skill_config),
                )
            )
        elif transport == "sse":
            result.append(
                SSEServerConfig(
                    name=server_name,
                    url=_expand(conf["url"], skill_config),
                    headers=_expand_dict(conf.get("headers", {}), skill_config),
                )
            )
        else:
            result.append(
                StdioServerConfig(
                    name=server_name,
                    command=_expand(conf["command"], skill_config),
                    args=_expand_list(conf.get("args", []), skill_config),
                    env=_expand_dict(conf.get("env", {}), skill_config),
                )
            )
    return result


def collect_mcp_configs(
    skill_names: list[str],
    skill_configs: dict | None = None,
) -> list[MCPServerConfig]:
    """Collect MCP configs from all enabled skills, dedup by server name."""
    seen: set[str] = set()
    configs: list[MCPServerConfig] = []
    for name in skill_names:
        per_skill = (skill_configs or {}).get(name, {})
        for conf in load_skill_mcp_config(name, per_skill):
            if conf.name not in seen:
                seen.add(conf.name)
                configs.append(conf)
    return configs
