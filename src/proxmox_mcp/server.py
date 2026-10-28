"""Proxmox VE MCP server (Mojood Connect demo): ask about VMs, containers, storage and backups in plain words, and
start, shut down, reboot or snapshot a guest with a confirmation step.

Run:  uv run --env-file .env proxmox-mcp          → http://127.0.0.1:8008/mcp (Streamable HTTP)
      uv run --env-file .env proxmox-mcp --stdio  → for Claude Desktop's config file

Settings (environment, written to .env by scripts/up.sh):
  PVE_URL           the Proxmox VE API (default https://127.0.0.1:8006)
  PVE_TOKEN_ID, PVE_TOKEN_SECRET  an API token (user@realm!name); the server acts with that user's rights
  PVE_VERIFY_TLS    "0" only for a self-signed local demo; default on
  PVE_SAMPLE        "1" (default) says in every session that the data is SAMPLE
  PVE_MCP_PORT      default 8008
"""

import logging
import os
import sys
from typing import Annotated, Literal

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from proxmox_mcp import tools
from proxmox_mcp.client import Proxmox

SAMPLE = os.environ.get("PVE_SAMPLE", "1") != "0"
_api: Proxmox | None = None


def api() -> Proxmox:
    global _api
    if _api is None:
        _api = Proxmox()
    return _api


INSTRUCTIONS = f"""\
Proxmox VE: the virtual machines (VMs) and LXC containers ("guests") on the nodes, their storage, backup jobs,
backups and snapshots, and Proxmox's task log.
{"This is a demo with SAMPLE data for a made-up company, Harbor Trading. " if SAMPLE else ""}\
Answer only from these tools; never guess a guest, a number or a date. Guests have an id (e.g. 101) and a name
(e.g. erp-db); use either. power_guest and snapshot_guest change real guests. Call one without `confirmation` first:
it changes nothing and returns what it would do plus a code. Show the user that summary and ask them to confirm.
Only after they say yes, call again with the same arguments and the code. Never confirm on the user's behalf. If a
tool returns `problem`, nothing changed: tell the user why. There is no tool to delete a guest, restore a backup or
roll back a snapshot: say so if asked."""

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)
SNAP = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

server = MCPServer(
    name="proxmox-mcp",
    title="Proxmox VE",
    description="VMs, containers, storage and backups in Proxmox VE: status, what needs attention, start/shutdown/reboot and snapshots with confirmation.",
    instructions=INSTRUCTIONS,
    website_url="https://mojood.app/connect",
    version="0.1.0",
)

CONFIRM = Field(description="leave empty first; pass the code only after the user confirms")


@server.tool(title="Overview", annotations=READ)
def overview() -> tools.Overview:
    """Nodes (CPU, memory, uptime), storage use, how many guests run or are stopped, and the backup jobs."""
    return tools.overview(api())


@server.tool(title="Find guests", annotations=READ)
def find_guests(
    query: Annotated[str | None, Field(description='part of a name, or an id, e.g. "erp"')] = None,
    status: Annotated[Literal["running", "stopped", "all"], Field(description="filter by state")] = "all",
    tag: Annotated[str | None, Field(description='a Proxmox tag, e.g. "prod"')] = None,
    kind: Annotated[Literal["vm", "container", "all"], Field(description="VMs, LXC containers or both")] = "all",
) -> tools.Guests:
    """VMs and containers with status, tags, CPU and memory in use now, and uptime."""
    return tools.find_guests(api(), query, status, tag, kind)


@server.tool(title="A guest in detail", annotations=READ)
def guest_details(guest: Annotated[str, Field(description='a name or id, e.g. "erp-db" or "101"')]) -> tools.Detail:
    """One guest: status and use, cores and disks, notes, its snapshots, its last backup and whether a backup job
    covers it, and its recent tasks from Proxmox's task log (who did what, when)."""
    return tools.guest_details(api(), guest)


@server.tool(title="What needs attention", annotations=READ)
def needs_attention(
    backup_days: Annotated[int, Field(description="a backup older than this many days counts as overdue", ge=1, le=90)] = 7,
) -> tools.Attention:
    """Guests no backup job covers, overdue backups, production guests that are stopped, old snapshots, storage
    filling up, and guests short of memory."""
    return tools.needs_attention(api(), backup_days)


@server.tool(title="Start, shut down or reboot a guest", annotations=CHANGE)
def power_guest(
    guest: Annotated[str, Field(description='a name or id, e.g. "mail-relay"')],
    action: Annotated[Literal["start", "shutdown", "reboot"], Field(description="shutdown and reboot go through the guest OS")],
    confirmation: Annotated[str | None, CONFIRM] = None,
) -> tools.Change:
    """Start, cleanly shut down, or reboot a VM or container. The first call changes nothing and returns a summary and
    a confirmation code; it happens only on a second call with the code, after the user has said yes."""
    return tools.power_guest(api(), guest, action, confirmation)


@server.tool(title="Take a snapshot", annotations=SNAP)
def snapshot_guest(
    guest: Annotated[str, Field(description='a name or id, e.g. "erp-db"')],
    name: Annotated[str, Field(description='letters, digits, "-" or "_", starting with a letter, e.g. "before-update"')],
    description: Annotated[str | None, Field(description="why, in a few words")] = None,
    confirmation: Annotated[str | None, CONFIRM] = None,
) -> tools.Change:
    """Take a snapshot of a VM or container (it keeps running). The first call changes nothing and returns a summary
    and a confirmation code; the snapshot is taken only on a second call with the code, after the user has said yes."""
    return tools.snapshot_guest(api(), guest, name, description, confirmation)


def app():
    security = TransportSecuritySettings(
        allowed_hosts=["127.0.0.1:*", "localhost:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
    )
    # Stateful sessions: a confirmation code must reach the same server process that issued it.
    return server.streamable_http_app(json_response=True, transport_security=security)


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per Proxmox call is noise in a client's log
    if "--stdio" in sys.argv:
        server.run("stdio")
        return
    uvicorn.run(app(), host="127.0.0.1", port=int(os.environ.get("PVE_MCP_PORT", "8008")), log_level="info",
                timeout_graceful_shutdown=5)
