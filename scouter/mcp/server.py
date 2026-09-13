"""The MCP adapter: a thin shell over the registry. No capability of its own."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
)

from scouter import registry
from scouter.errors import ScouterError
from scouter.mcp import schemas

logger = logging.getLogger(__name__)


class ScouterServer:
    """Exposes exactly the registry's verbs, with handwritten schemas."""

    def __init__(self) -> None:
        self.app: Server[Any] = Server(
            "scouter",
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )
        self._by_mcp_name = {v.mcp_name: v.id for v in registry.load_verbs().values()}

    async def _on_list_tools(
        self, ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=schemas.tools())

    async def _on_call_tool(
        self, ctx: ServerRequestContext[Any], params: CallToolRequestParams
    ) -> CallToolResult:
        text = self.call(params.name, params.arguments or {})
        return CallToolResult(content=[TextContent(type="text", text=text)])

    def call(self, tool_name: str, arguments: dict) -> str:
        """Run one tool and return the envelope JSON the CLI would have printed.

        Errors come back as readable tool output, never as a transport error:
        the agent has to be able to act on `fix`.
        """
        verb_id = self._by_mcp_name.get(tool_name)
        if verb_id is None:
            return json.dumps(
                {
                    "ok": False,
                    "outcome": "error:unknown_tool",
                    "error": {
                        "code": "unknown_tool",
                        "message": f"unknown tool {tool_name!r}",
                        "fix": "call tools/list",
                    },
                },
                sort_keys=True,
            )
        try:
            envelope = registry.call(verb_id, "mcp", **arguments)
        except ScouterError as e:
            # Defensive: registry.call already catches ScouterError raised by
            # the verb itself and turns it into an Envelope with ok=False, so
            # this branch only guards a ScouterError raised outside that
            # (e.g. from registry.call's own setup code).
            return json.dumps(
                {
                    "verb": verb_id,
                    "layer": "mcp",
                    "ok": False,
                    "outcome": f"error:{e.code}",
                    "error": e.to_dict(),
                },
                sort_keys=True,
            )
        except (TypeError, ValueError) as e:
            logger.exception("tool %s rejected its arguments", tool_name)
            return json.dumps(
                {
                    "verb": verb_id,
                    "layer": "mcp",
                    "ok": False,
                    "outcome": "error:usage",
                    "error": {
                        "code": "usage",
                        "message": str(e),
                        "fix": "check the tool's inputSchema",
                    },
                },
                sort_keys=True,
            )
        except Exception as e:
            # Catch-all so an unexpected crash reaches the agent as readable
            # tool content (which it can act on) rather than a JSON-RPC
            # internal-error transport failure it can't see into.
            logger.exception("tool %s crashed", tool_name)
            return json.dumps(
                {
                    "verb": verb_id,
                    "layer": "mcp",
                    "ok": False,
                    "outcome": "error",
                    "error": {
                        "code": "error",
                        "message": str(e),
                        "fix": "check the server logs",
                    },
                },
                sort_keys=True,
            )
        return json.dumps(envelope.to_dict(), sort_keys=True)

    def tool_names(self) -> list[str]:
        return [spec["name"] for spec in schemas.TOOL_SPECS]

    async def run(self) -> None:
        async with stdio_server() as (read, write):
            await self.app.run(read, write, self.app.create_initialization_options())


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(ScouterServer().run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
