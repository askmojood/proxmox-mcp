"""The six tools, as plain functions over a Proxmox client (the MCP wiring is in server.py).

Four only read. The two that change something (start, shut down or reboot a guest; take a snapshot) never act on
the first call: they return a preview and a one-time confirmation code, and act only when called again with that
code and the same request. Proxmox's own task log records every change, under the API token's user.
"""

import re
import secrets
import time
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from proxmox_mcp.client import Proxmox, ProxmoxError

CONFIRM_TTL_S = 10 * 60
OLD_SNAPSHOT_DAYS = 30
STORAGE_WARN = 80  # percent used
MEMORY_WARN = 90  # percent of a running guest's memory in use
SNAPNAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,39}")
VIA = "via Claude (Mojood Connect demo)"


def _days(epoch: float | None) -> float | None:
    return round((time.time() - epoch) / 86400, 1) if epoch else None


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="minutes") if epoch else None


def _pct(used: float | None, total: float | None) -> float | None:
    return round(100 * used / total, 1) if used is not None and total else None


class Guest(BaseModel):
    id: int
    name: str
    kind: str = Field(description="VM or container")
    status: str = Field(description="running or stopped")
    tags: list[str] = Field(default_factory=list)
    node: str
    cpu_percent: float | None = Field(None, description="share of its CPU cores in use now")
    memory_used_mb: int | None = None
    memory_mb: int | None = None
    uptime_hours: float | None = None


def _guest(r: dict) -> Guest:
    running = r.get("status") == "running"
    return Guest(id=r["vmid"], name=r.get("name") or f"guest {r['vmid']}",
                 kind="VM" if r["type"] == "qemu" else "container", status=r.get("status", "unknown"),
                 tags=[t for t in (r.get("tags") or "").split(";") if t], node=r["node"],
                 cpu_percent=round(100 * r.get("cpu", 0), 1) if running else None,
                 memory_used_mb=r.get("mem", 0) // 2**20 if running else None,
                 memory_mb=r.get("maxmem", 0) // 2**20 or None,
                 uptime_hours=round(r.get("uptime", 0) / 3600, 1) if running else None)


def _kind(g: Guest) -> str:
    return "qemu" if g.kind == "VM" else "lxc"


def guests(api: Proxmox) -> list[Guest]:
    rows = api.get("cluster/resources", type="vm") or []
    return sorted((_guest(r) for r in rows if r.get("template") != 1), key=lambda g: g.id)


def _resolve(api: Proxmox, who: str | int) -> tuple[Guest | None, list[Guest]]:
    """One guest by id or name; otherwise None and the candidates (several matches, or none)."""
    allg = guests(api)
    s = str(who).strip().lower()
    if s.isdigit():
        hit = [g for g in allg if g.id == int(s)]
        return (hit[0], []) if hit else (None, [])
    exact = [g for g in allg if g.name.lower() == s]
    if len(exact) == 1:
        return exact[0], []
    part = [g for g in allg if s in g.name.lower()]
    return (part[0], []) if len(part) == 1 else (None, part)


def _not_found(who: str | int, candidates: list[Guest]) -> str:
    if candidates:
        return f'"{who}" matches several guests: ' + ", ".join(f"{g.name} ({g.id})" for g in candidates) + ". Which one?"
    return f'No VM or container is called "{who}".'


def _backups(api: Proxmox) -> dict[int, dict]:
    """The newest backup of each guest, from every storage that holds backups."""
    node = api.node()
    newest: dict[int, dict] = {}
    for st in api.get(f"nodes/{node}/storage", content="backup") or []:
        try:
            items = api.get(f"nodes/{node}/storage/{st['storage']}/content", content="backup") or []
        except ProxmoxError:
            continue
        for b in items:
            vmid = int(b.get("vmid") or 0)
            if vmid and b.get("ctime", 0) > newest.get(vmid, {}).get("ctime", 0):
                newest[vmid] = b
    return newest


def _jobs(api: Proxmox) -> list[dict]:
    return api.get("cluster/backup") or []


def _uncovered(api: Proxmox) -> set[int]:
    return {int(g["vmid"]) for g in api.get("cluster/backup-info/not-backed-up") or []}


def _snapshots(api: Proxmox, g: Guest) -> list[dict]:
    return [s for s in api.get(f"nodes/{g.node}/{_kind(g)}/{g.id}/snapshot") or [] if s.get("name") != "current"]


# ---------- read ----------

class Node(BaseModel):
    name: str
    status: str
    cpu_percent: float | None
    memory_used_gb: float | None
    memory_gb: float | None
    uptime_days: float | None


class Storage(BaseModel):
    name: str
    type: str
    used_gb: float | None
    size_gb: float | None
    used_percent: float | None


class Job(BaseModel):
    id: str
    schedule: str | None
    enabled: bool
    guests: str = Field(description='guest ids, or "all"')
    storage: str | None


class Overview(BaseModel):
    nodes: list[Node]
    storage: list[Storage]
    running: int
    stopped: int
    vms: int
    containers: int
    backup_jobs: list[Job]


def overview(api: Proxmox) -> Overview:
    rows = api.get("cluster/resources") or []
    gb = 2**30
    nodes = [Node(name=r["node"], status=r.get("status", "unknown"), cpu_percent=round(100 * r.get("cpu", 0), 1),
                  memory_used_gb=round(r.get("mem", 0) / gb, 1), memory_gb=round(r.get("maxmem", 0) / gb, 1),
                  uptime_days=round(r.get("uptime", 0) / 86400, 1)) for r in rows if r.get("type") == "node"]
    storage = [Storage(name=r["storage"], type=r.get("plugintype", ""), used_gb=round(r.get("disk", 0) / gb, 1),
                       size_gb=round(r.get("maxdisk", 0) / gb, 1), used_percent=_pct(r.get("disk"), r.get("maxdisk")))
               for r in rows if r.get("type") == "storage" and r.get("status") == "available"]
    gs = [_guest(r) for r in rows if r.get("type") in ("qemu", "lxc") and r.get("template") != 1]
    jobs = [Job(id=j.get("id", ""), schedule=j.get("schedule"), enabled=bool(j.get("enabled", 1)),
                guests="all" if j.get("all") else str(j.get("vmid", "")), storage=j.get("storage")) for j in _jobs(api)]
    return Overview(nodes=nodes, storage=storage, running=sum(g.status == "running" for g in gs),
                    stopped=sum(g.status != "running" for g in gs), vms=sum(g.kind == "VM" for g in gs),
                    containers=sum(g.kind == "container" for g in gs), backup_jobs=jobs)


class Guests(BaseModel):
    guests: list[Guest]
    total: int


def find_guests(api: Proxmox, query: str | None = None, status: str = "all", tag: str | None = None,
                kind: str = "all") -> Guests:
    q, t = (query or "").lower(), (tag or "").lower()
    hits = [g for g in guests(api)
            if (not q or q in g.name.lower() or q == str(g.id))
            and (status == "all" or (g.status == "running") == (status == "running"))
            and (not t or t in [x.lower() for x in g.tags])
            and (kind == "all" or g.kind.lower() == kind)]
    return Guests(guests=hits[:50], total=len(hits))


class Snapshot(BaseModel):
    name: str
    taken: str | None
    age_days: float | None
    description: str | None


class Backup(BaseModel):
    when: str | None
    age_days: float | None
    size_mb: int | None
    storage: str


class Task(BaseModel):
    what: str
    when: str | None
    result: str | None
    by: str | None


class Detail(BaseModel):
    guest: Guest | None = None
    cores: int | None = None
    disks: list[str] = Field(default_factory=list)
    starts_at_boot: bool | None = None
    notes: str | None = None
    snapshots: list[Snapshot] = Field(default_factory=list)
    last_backup: Backup | None = None
    in_backup_job: bool | None = None
    recent_tasks: list[Task] = Field(default_factory=list, description="newest first, from Proxmox's task log")
    problem: str | None = None


TASKS = {"qmstart": "started", "qmshutdown": "shut down", "qmreboot": "rebooted", "qmstop": "stopped (hard)",
         "vzstart": "started", "vzshutdown": "shut down", "vzreboot": "rebooted", "vzstop": "stopped (hard)",
         "qmsnapshot": "snapshot taken", "vzsnapshot": "snapshot taken", "vzdump": "backed up",
         "qmcreate": "created", "vzcreate": "created", "qmrollback": "rolled back", "vzrollback": "rolled back"}

DISK = re.compile(r"(scsi|virtio|sata|ide|mp)\d+$|rootfs$")


def guest_details(api: Proxmox, guest: str | int) -> Detail:
    g, cands = _resolve(api, guest)
    if not g:
        return Detail(problem=_not_found(guest, cands))
    cfg = api.get(f"nodes/{g.node}/{_kind(g)}/{g.id}/config") or {}
    disks = [f"{k}: {v.split(',')[0]} ({m.group(1)})" if (m := re.search(r"size=([0-9.]+[KMGT]?)", v)) else f"{k}: {v.split(',')[0]}"
             for k, v in cfg.items() if DISK.match(k) and "media=cdrom" not in str(v)]
    snaps = [Snapshot(name=s["name"], taken=_iso(s.get("snaptime")), age_days=_days(s.get("snaptime")),
                      description=s.get("description") or None) for s in _snapshots(api, g)]
    b = _backups(api).get(g.id)
    tasks = api.get(f"nodes/{g.node}/tasks", vmid=g.id, limit=8) or []
    return Detail(guest=g, cores=cfg.get("cores"), disks=disks, starts_at_boot=bool(cfg.get("onboot", 0)),
                  notes=(cfg.get("description") or "").strip() or None, snapshots=snaps,
                  last_backup=Backup(when=_iso(b["ctime"]), age_days=_days(b["ctime"]), size_mb=(b.get("size") or 0) // 2**20,
                                     storage=b["volid"].split(":")[0]) if b else None,
                  in_backup_job=g.id not in _uncovered(api),
                  recent_tasks=[Task(what=TASKS.get(t.get("type", ""), t.get("type")), when=_iso(t.get("starttime")),
                                     result=t.get("status"), by=t.get("user")) for t in tasks])


class Issue(BaseModel):
    guest: str
    id: int | None = None
    detail: str


class Attention(BaseModel):
    not_in_any_backup_job: list[Issue] = Field(description="guests no backup job covers")
    backups_overdue: list[Issue] = Field(description="in a backup job, but no backup newer than the limit")
    production_stopped: list[Issue] = Field(description='tagged "prod" and not running')
    old_snapshots: list[Issue]
    storage_filling: list[Issue]
    memory_high: list[Issue]


def needs_attention(api: Proxmox, backup_days: int = 7) -> Attention:
    gs = guests(api)
    uncovered = _uncovered(api)
    newest = _backups(api)
    label = {g.id: f"{g.name} ({g.kind}, {g.id})" for g in gs}
    overdue = []
    for g in gs:
        if g.id in uncovered:
            continue
        b = newest.get(g.id)
        if not b:
            overdue.append(Issue(guest=label[g.id], id=g.id, detail="in a backup job but has never been backed up"))
        elif (age := _days(b["ctime"])) > backup_days:
            overdue.append(Issue(guest=label[g.id], id=g.id, detail=f"last backup {age} days ago ({_iso(b['ctime'])})"))
    old = []
    for g in gs:
        for s in _snapshots(api, g):
            if (age := _days(s.get("snaptime"))) and age > OLD_SNAPSHOT_DAYS:
                old.append(Issue(guest=label[g.id], id=g.id, detail=f'snapshot "{s["name"]}" is {age} days old'))
    ov = overview(api)
    return Attention(
        not_in_any_backup_job=[Issue(guest=label[g.id], id=g.id, detail=f"{g.status}; tags: {', '.join(g.tags) or 'none'}")
                               for g in gs if g.id in uncovered],
        backups_overdue=sorted(overdue, key=lambda i: i.detail),
        production_stopped=[Issue(guest=label[g.id], id=g.id, detail="stopped") for g in gs
                            if "prod" in g.tags and g.status != "running"],
        old_snapshots=old,
        storage_filling=[Issue(guest=s.name, detail=f"{s.used_percent}% used of {s.size_gb} GB") for s in ov.storage
                         if (s.used_percent or 0) >= STORAGE_WARN],
        memory_high=[Issue(guest=label[g.id], id=g.id, detail=f"{_pct(g.memory_used_mb, g.memory_mb)}% of {g.memory_mb} MB")
                     for g in gs if g.status == "running" and (_pct(g.memory_used_mb, g.memory_mb) or 0) >= MEMORY_WARN])


# ---------- change (preview, then confirm) ----------

class Change(BaseModel):
    done: bool = Field(description="false: nothing has changed yet; true: Proxmox has done it")
    summary: str
    confirmation: str | None = Field(None, description="when done is false: the code to pass back, only after the user says yes")
    expires_in_minutes: int | None = None
    guest: Guest | None = None
    problem: str | None = Field(None, description="why it can't be done; nothing was changed")


_pending: dict[str, tuple[float, tuple]] = {}


def _ask(request: tuple, summary: str, g: Guest) -> Change:
    now = time.monotonic()
    for k in [k for k, (t, _) in _pending.items() if now - t > CONFIRM_TTL_S]:
        del _pending[k]
    code = secrets.token_hex(3)
    _pending[code] = (now, request)
    return Change(done=False, summary=summary + " Nothing has changed yet: ask the user to confirm.",
                  confirmation=code, expires_in_minutes=CONFIRM_TTL_S // 60, guest=g)


def _confirmed(code: str, request: tuple) -> bool:
    held = _pending.get(code.strip())
    if not held or time.monotonic() - held[0] > CONFIRM_TTL_S or held[1] != request:
        return False
    del _pending[code.strip()]
    return True


def _expired(summary: str, g: Guest) -> Change:
    return Change(done=False, summary=summary, guest=g,
                  problem="That confirmation code doesn't match this request or has expired. Ask again.")


def _after(api: Proxmox, g: Guest) -> Guest:
    """The guest's state right after a change, read live (the cluster summary lags by a few seconds)."""
    cur = api.get(f"nodes/{g.node}/{_kind(g)}/{g.id}/status/current") or {}
    return _guest({"vmid": g.id, "type": _kind(g), "node": g.node, "name": g.name, "tags": ";".join(g.tags), **cur})


ACTIONS = {"start": "Start", "shutdown": "Shut down (cleanly, through the guest OS)", "reboot": "Reboot"}


def power_guest(api: Proxmox, guest: str | int, action: str, confirmation: str | None = None) -> Change:
    if action not in ACTIONS:
        return Change(done=False, summary="", problem=f'"{action}" isn\'t one of: start, shutdown, reboot.')
    g, cands = _resolve(api, guest)
    if not g:
        return Change(done=False, summary="", problem=_not_found(guest, cands))
    if action == "start" and g.status == "running":
        return Change(done=False, summary="", guest=g, problem=f"{g.name} is already running.")
    if action in ("shutdown", "reboot") and g.status != "running":
        return Change(done=False, summary="", guest=g, problem=f"{g.name} isn't running ({g.status}).")
    request = ("power", g.id, action)
    up = f", up {g.uptime_hours} hours" if g.uptime_hours is not None else ""
    prod = " It is tagged prod." if "prod" in g.tags else ""
    summary = f"{ACTIONS[action]} {g.name} ({g.kind} {g.id} on {g.node}, now {g.status}{up}).{prod}"
    if not confirmation:
        return _ask(request, summary, g)
    if not _confirmed(confirmation, request):
        return _expired(summary, g)
    upid = api.post(f"nodes/{g.node}/{_kind(g)}/{g.id}/status/{action}")
    result = api.wait(g.node, upid, timeout_s=120)
    if result != "OK":
        return Change(done=False, summary=summary, guest=_after(api, g), problem=f"Proxmox's task ended with: {result}")
    word = {"start": "started", "shutdown": "shut down", "reboot": "rebooted"}[action]
    return Change(done=True, summary=f"Done: {g.name} is {word}. Proxmox's task log records it.", guest=_after(api, g))


def snapshot_guest(api: Proxmox, guest: str | int, name: str, description: str | None = None,
                   confirmation: str | None = None) -> Change:
    g, cands = _resolve(api, guest)
    if not g:
        return Change(done=False, summary="", problem=_not_found(guest, cands))
    if not SNAPNAME.fullmatch(name):
        return Change(done=False, summary="", guest=g, problem="A snapshot name starts with a letter and has 2–40 "
                      'letters, digits, "-" or "_" (e.g. before-update).')
    if any(s["name"] == name for s in _snapshots(api, g)):
        return Change(done=False, summary="", guest=g, problem=f'{g.name} already has a snapshot called "{name}".')
    note = f"{description.strip().rstrip('.')}. Taken {VIA}" if description and description.strip() else f"Taken {VIA}"
    request = ("snapshot", g.id, name, note)
    summary = f'Take a snapshot "{name}" of {g.name} ({g.kind} {g.id}, {g.status}), noted "{note}". The guest keeps running.'
    if not confirmation:
        return _ask(request, summary, g)
    if not _confirmed(confirmation, request):
        return _expired(summary, g)
    upid = api.post(f"nodes/{g.node}/{_kind(g)}/{g.id}/snapshot", {"snapname": name, "description": note})
    result = api.wait(g.node, upid, timeout_s=120)
    if result != "OK":
        return Change(done=False, summary=summary, guest=g, problem=f"Proxmox's task ended with: {result}")
    return Change(done=True, summary=f'Done: {g.name} has a snapshot "{name}". Proxmox\'s task log records it.', guest=g)
