"""Restrict the server to an allowlist of apps.

A service account often has access to several apps in a Play Console account,
but a given MCP setup is usually meant for one of them. When
``PLAY_STORE_MCP_ALLOWED_PACKAGES`` is set (comma or space separated package
names), this middleware rejects any tool call whose ``package_name`` is not on
the list, before the tool runs. It sits above the tools, so with code-mode on
it also checks the inner calls made by ``execute``.

Account-wide user management (create/update/delete a Play Console user) can't
be limited to one app, so it is rejected too while an allowlist is set.
Per-app grants (create_grant & co.) carry a package_name and are checked like
any other tool.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

import structlog
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

if TYPE_CHECKING:
    import mcp.types as mt
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from fastmcp.tools import ToolResult

logger = structlog.get_logger(__name__)

ALLOWED_ENV = "PLAY_STORE_MCP_ALLOWED_PACKAGES"

# Tools that change access to every app in the developer account.
ACCOUNT_WIDE_WRITE_TOOLS = frozenset({"create_user", "update_user", "delete_user"})


def allowed_packages() -> frozenset[str] | None:
    """Return the allowed package names, or None when no allowlist is configured."""
    raw = os.environ.get(ALLOWED_ENV, "")
    names = frozenset(part for part in re.split(r"[\s,]+", raw.strip()) if part)
    return names or None


def check_call(tool: str, arguments: dict[str, object] | None) -> str | None:
    """Return why a tool call is outside the allowlist, or None if it may run."""
    allowed = allowed_packages()
    if allowed is None:
        return None
    if tool in ACCOUNT_WIDE_WRITE_TOOLS:
        return (
            f"{tool} changes access to every app in the developer account and is disabled "
            f"while {ALLOWED_ENV} limits this server to: {', '.join(sorted(allowed))}"
        )
    package = (arguments or {}).get("package_name")
    if package is not None and package not in allowed:
        return (
            f"Package {package!r} is not allowed. This server is limited to: "
            f"{', '.join(sorted(allowed))} (set by {ALLOWED_ENV})."
        )
    return None


class PackageAllowlistMiddleware(Middleware):
    """Reject tool calls for apps outside PLAY_STORE_MCP_ALLOWED_PACKAGES."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        params = context.message
        if reason := check_call(params.name, params.arguments):
            logger.warning("Blocked tool call outside package allowlist", tool=params.name)
            raise ToolError(reason)
        return await call_next(context)
