"""
MCP client manager — connects to MCP servers, discovers tools, routes calls.

Gracefully degrades if the `mcp` package is not installed.
"""

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamablehttp_client

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    logger.info("mcp package not installed, MCP support disabled")

from mcp_config import (
    MCPServerConfig,
    SSEServerConfig,
    StreamableHTTPServerConfig,
    StdioServerConfig,
)

TOOL_PREFIX = "mcp"
SEP = "__"
CONNECT_TIMEOUT = int(os.environ.get("MCP_CONNECT_TIMEOUT", "15"))
CALL_TIMEOUT = int(os.environ.get("MCP_CALL_TIMEOUT", "60"))


@dataclass
class MCPToolInfo:
    server_name: str
    original_name: str
    qualified_name: str
    description: str
    input_schema: dict


@dataclass
class MCPConnection:
    server_name: str
    session: Any  # ClientSession
    tools: list[MCPToolInfo] = field(default_factory=list)


class MCPManager:
    """
    Manage multiple MCP server connections.

    Usage:
        mgr = MCPManager()
        await mgr.initialize(configs)
        tools = mgr.get_anthropic_tools()   # merge with native tools
        result = await mgr.call_tool("mcp__fs__read_file", {...})
        await mgr.shutdown()
    """

    def __init__(self):
        self._connections: dict[str, MCPConnection] = {}
        self._tool_index: dict[str, MCPToolInfo] = {}
        self._exit_stack = AsyncExitStack()

    @staticmethod
    def available() -> bool:
        return MCP_AVAILABLE

    @staticmethod
    def qualify_name(server_name: str, tool_name: str) -> str:
        return f"{TOOL_PREFIX}{SEP}{server_name}{SEP}{tool_name}"

    def is_mcp_tool(self, name: str) -> bool:
        return name in self._tool_index

    async def initialize(self, configs: list[MCPServerConfig]):
        """Connect to all MCP servers in parallel."""
        if not MCP_AVAILABLE:
            return
        results = await asyncio.gather(
            *[self._connect(conf) for conf in configs],
            return_exceptions=True,
        )
        for conf, result in zip(configs, results):
            if isinstance(result, Exception):
                logger.warning("MCP '%s' connection failed: %s", conf.name, result)

    async def _connect(self, conf: MCPServerConfig):
        if isinstance(conf, StdioServerConfig):
            params = StdioServerParameters(
                command=conf.command,
                args=conf.args,
                env={**os.environ, **conf.env} if conf.env else None,
            )
            transport = await self._exit_stack.enter_async_context(
                stdio_client(params)
            )
        elif isinstance(conf, StreamableHTTPServerConfig):
            # streamablehttp_client yields (read, write, get_session_id)
            read_stream, write_stream, _ = await self._exit_stack.enter_async_context(
                streamablehttp_client(
                    conf.url, headers=conf.headers if conf.headers else None
                )
            )
            session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await asyncio.wait_for(session.initialize(), timeout=CONNECT_TIMEOUT)

            tools_resp = await session.list_tools()
            conn = MCPConnection(server_name=conf.name, session=session)

            for tool in tools_resp.tools:
                qname = self.qualify_name(conf.name, tool.name)
                if len(qname) > 64:
                    logger.warning(
                        "MCP tool name too long (%d chars), skipping: %s",
                        len(qname),
                        qname,
                    )
                    continue
                info = MCPToolInfo(
                    server_name=conf.name,
                    original_name=tool.name,
                    qualified_name=qname,
                    description=tool.description or "",
                    input_schema=tool.inputSchema,
                )
                conn.tools.append(info)
                self._tool_index[qname] = info

            self._connections[conf.name] = conn
            logger.info(
                "MCP '%s' connected, %d tools: %s",
                conf.name,
                len(conn.tools),
                [t.original_name for t in conn.tools],
            )
            return
        elif isinstance(conf, SSEServerConfig):
            transport = await self._exit_stack.enter_async_context(
                sse_client(conf.url, headers=conf.headers if conf.headers else None)
            )
        else:
            logger.warning("Unknown MCP config type: %s", type(conf))
            return

        read_stream, write_stream = transport
        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await asyncio.wait_for(session.initialize(), timeout=CONNECT_TIMEOUT)

        tools_resp = await session.list_tools()
        conn = MCPConnection(server_name=conf.name, session=session)

        for tool in tools_resp.tools:
            qname = self.qualify_name(conf.name, tool.name)
            if len(qname) > 64:
                logger.warning(
                    "MCP tool name too long (%d chars), skipping: %s",
                    len(qname),
                    qname,
                )
                continue
            info = MCPToolInfo(
                server_name=conf.name,
                original_name=tool.name,
                qualified_name=qname,
                description=tool.description or "",
                input_schema=tool.inputSchema,
            )
            conn.tools.append(info)
            self._tool_index[qname] = info

        self._connections[conf.name] = conn
        logger.info(
            "MCP '%s' connected, %d tools: %s",
            conf.name,
            len(conn.tools),
            [t.original_name for t in conn.tools],
        )

    def get_anthropic_tools(self) -> list[dict]:
        """Convert all MCP tools to Anthropic API tool format."""
        return [
            {
                "name": info.qualified_name,
                "description": f"[MCP:{info.server_name}] {info.description}",
                "input_schema": info.input_schema,
            }
            for info in self._tool_index.values()
        ]

    async def call_tool(self, qualified_name: str, arguments: dict) -> dict:
        """Call an MCP tool. Returns {"ok": True/False, "data"/"error": ...}."""
        info = self._tool_index.get(qualified_name)
        if not info:
            return {"ok": False, "error": f"Unknown MCP tool: {qualified_name}"}

        conn = self._connections.get(info.server_name)
        if not conn:
            return {
                "ok": False,
                "error": f"MCP server '{info.server_name}' not connected",
            }

        try:
            result = await asyncio.wait_for(
                conn.session.call_tool(info.original_name, arguments),
                timeout=CALL_TIMEOUT,
            )
            content = _format_content(result.content)
            if result.isError:
                return {"ok": False, "error": content}
            return {"ok": True, "data": content}
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": f"MCP tool '{info.original_name}' timed out after {CALL_TIMEOUT}s",
            }
        except Exception as exc:
            return {"ok": False, "error": f"MCP tool error: {exc}"}

    async def shutdown(self):
        """Close all MCP server connections."""
        try:
            await self._exit_stack.aclose()
        except Exception:
            logger.exception("Error shutting down MCP connections")
        self._connections.clear()
        self._tool_index.clear()


def _format_content(content_list) -> str:
    """Convert MCP result content to a plain string."""
    parts = []
    for item in content_list:
        if hasattr(item, "text"):
            parts.append(item.text)
        elif hasattr(item, "type") and item.type == "image":
            parts.append(f"[image: {getattr(item, 'mimeType', 'unknown')}]")
        else:
            parts.append(str(item))
    return "\n".join(parts)
