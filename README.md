# Proxmox VE MCP server

Ask Claude about your Proxmox VE in plain words: which guests no backup job covers, whose backups are overdue,
which production VMs are stopped, what's going on with one VM. Start, shut down or reboot a guest and take snapshots
from the same chat, with a confirmation step before anything changes. There is no tool that deletes a guest,
restores a backup or rolls back a snapshot.

Built by [Mojood Connect](https://mojood.app/connect) as a working demo for [Proxmox](https://www.proxmox.com). It
runs against any Proxmox VE 8 or 9 through its REST API, with an API token. This repository includes a script that
sets up a local Proxmox VE 9.2 (Proxmox's official arm64 build, in a VM on an Apple Silicon Mac) with **SAMPLE
guests** for a made-up company, Harbor Trading, so you can try it without touching a real cluster.

## Tools

| Tool | What it does | Changes anything? |
| --- | --- | --- |
| `overview` | Nodes (CPU, memory, uptime), storage use, guests running and stopped, backup jobs | No |
| `find_guests` | VMs and containers by name, id, tag or state, with CPU and memory in use | No |
| `guest_details` | One guest: cores, disks, notes, snapshots, last backup, whether a backup job covers it, recent tasks (who did what) | No |
| `needs_attention` | Guests in no backup job, overdue backups, stopped production guests, old snapshots, storage filling up, guests short of memory | No |
| `power_guest` | Start, shut down (cleanly) or reboot a VM or container | Yes, after confirmation |
| `snapshot_guest` | Take a snapshot, with a note saying who asked for it | Yes, after confirmation |

**How changes are made safe.** A call to `power_guest` or `snapshot_guest` without a `confirmation` changes nothing:
it returns a plain summary ("Start mail-relay (VM 104 on lima-pve, now stopped). It is tagged prod.") and a one-time
code. The change happens only when the same request comes back with that code, within 10 minutes. A code works once,
and only for the request it was issued for. The server's instructions tell the model to show the summary and wait
for the user's yes. Proxmox's own task log records every change under the token's user.

**Least privilege.** The demo's token belongs to its own Proxmox user, `claude@pve`, with a custom role holding only
`VM.Audit`, `VM.PowerMgmt`, `VM.Snapshot`, `Datastore.Audit` and `Sys.Audit`, plus `VM.Backup` and
`Datastore.AllocateSpace` (on the backup storage only), because Proxmox lists a guest's backups only to users who
could back it up. No tool makes, restores or deletes a backup.

## Try it locally

Needs an Apple Silicon Mac (M3 or later, for nested virtualization) with [Lima](https://lima-vm.io), [uv](https://docs.astral.sh/uv/)
and Python 3.13, about 6 GB of free memory and 40 GB of disk.

```
scripts/reset.sh                         # Proxmox VE 9.2 in a Lima VM (first run installs it), filled with SAMPLE guests
uv run --env-file .env proxmox-mcp       # MCP server on http://127.0.0.1:8008/mcp (Streamable HTTP)
```

`scripts/up.sh` installs Proxmox VE the way Proxmox's
[Debian 13 guide](https://pve.proxmox.com/wiki/Install_Proxmox_VE_on_Debian_13_Trixie) describes, adds a ZFS pool
(on a file) so VMs and containers can be snapshotted, creates the `claude@pve` user and its token, and a read-only
`viewer@pve` user for the web UI at https://127.0.0.1:8006. Secrets go only into the git-ignored `.env` and are never
printed. The local certificate is self-signed, so `.env` sets `PVE_VERIFY_TLS=0`; keep it on for a real installation.

**Claude Desktop:** add this to `claude_desktop_config.json` (Settings › Developer), with the path to this folder:

```json
{
  "mcpServers": {
    "proxmox": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/proxmox-mcp", "--env-file", "/path/to/proxmox-mcp/.env", "proxmox-mcp", "--stdio"]
    }
  }
}
```

Then ask: "Which servers aren't backed up?", "What's going on with erp-app?", "Snapshot erp-app before tonight's
update, and start mail-relay again."

**Your own Proxmox VE:** create a user and an API token with the rights above
(`pveum user token add <user> <name> --privsep 0`), then set `PVE_URL`, `PVE_TOKEN_ID` (`user@realm!name`),
`PVE_TOKEN_SECRET` and `PVE_SAMPLE=0`.

## The SAMPLE guests

Six VMs (Alpine Linux, really booting) and four LXC containers for Harbor Trading: an ERP database and app server, a
file server, a mail relay that was left stopped, a retired CRM with a 96-day-old snapshot, a staging web server, two
web containers, monitoring and a VPN gateway. A nightly backup job covers most of them but not all, and one guest's
newest backup is 9.9 days old. Backups and snapshots are real; `scripts/seed.sh` moves a few of their timestamps back
to give the estate a history. Everything in it is made up.

## Tests

```
uv run --env-file .env pytest            # unit tests (a fake Proxmox API), plus live tests when the local Proxmox runs
```

## What a production version adds

This demo uses one API token. A production server for Proxmox customers would add sign-in with each person's own
Proxmox account (their realm, LDAP or OpenID), so Claude sees and does exactly what their roles allow; clusters with
several nodes; Proxmox Backup Server; hosting with an audit log of every tool call; and a directory listing. That is
what [Mojood Connect](https://mojood.app/connect) builds.

## License

Apache 2.0. Proxmox is a registered trademark of Proxmox Server Solutions GmbH; this project is not made or endorsed
by Proxmox Server Solutions GmbH.
