"""Unit tests over a fake Proxmox VE API (httpx.MockTransport): no VM needed."""

import time

import httpx
import pytest

from proxmox_mcp import tools
from proxmox_mcp.client import Proxmox, ProxmoxError

NOW = time.time()
DAY = 86400


class Fake:
    def __init__(self):
        self.guests = [
            {"vmid": 101, "type": "qemu", "name": "erp-db", "status": "running", "tags": "erp;prod", "node": "pve1",
             "cpu": 0.12, "mem": 300 * 2**20, "maxmem": 512 * 2**20, "uptime": 7200},
            {"vmid": 104, "type": "qemu", "name": "mail-relay", "status": "stopped", "tags": "prod", "node": "pve1",
             "maxmem": 384 * 2**20},
            {"vmid": 201, "type": "lxc", "name": "web-01", "status": "running", "tags": "prod;web", "node": "pve1",
             "cpu": 0.01, "mem": 20 * 2**20, "maxmem": 128 * 2**20, "uptime": 3600},
        ]
        self.posts: list[tuple[str, dict]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api2/json/")
        ok = lambda data: httpx.Response(200, json={"data": data})
        if req.method == "POST":
            self.posts.append((path, dict(httpx.QueryParams(req.content.decode()))))
            return ok("UPID:pve1:1:task")
        if path == "cluster/resources":
            node = {"type": "node", "node": "pve1", "status": "online", "cpu": 0.2, "mem": 2 * 2**30, "maxmem": 6 * 2**30,
                    "uptime": 3 * DAY}
            store = {"type": "storage", "storage": "tank", "status": "available", "plugintype": "zfspool",
                     "disk": 85 * 2**30, "maxdisk": 100 * 2**30}
            vms = [{**g, "type": g["type"]} for g in self.guests]
            return ok(vms if req.url.params.get("type") == "vm" else [node, store, *vms])
        if path == "nodes":
            return ok([{"node": "pve1"}])
        if path == "cluster/backup":
            return ok([{"id": "nightly", "schedule": "01:00", "enabled": 1, "vmid": "101,201", "storage": "local"}])
        if path == "cluster/backup-info/not-backed-up":
            return ok([{"vmid": 104, "name": "mail-relay", "type": "qemu"}])
        if path == "nodes/pve1/storage":
            return ok([{"storage": "local"}])
        if path == "nodes/pve1/storage/local/content":
            return ok([{"vmid": 101, "ctime": NOW - 9 * DAY, "volid": "local:backup/vzdump-qemu-101.vma.zst", "size": 2**27},
                       {"vmid": 201, "ctime": NOW - 0.5 * DAY, "volid": "local:backup/vzdump-lxc-201.tar.zst", "size": 2**22}])
        if path.endswith("/snapshot"):
            old = {"name": "pre-migration", "snaptime": NOW - 96 * DAY, "description": "old"}
            return ok([old, {"name": "current"}] if "/104/" in path else [{"name": "current"}])
        if path.endswith("/config"):
            return ok({"cores": 2, "onboot": 1, "description": "SAMPLE", "scsi0": "tank:vm-101-disk-1,size=8G"})
        if path == "nodes/pve1/tasks":
            return ok([{"type": "qmstart", "starttime": NOW - 60, "status": "OK", "user": "claude@pve!connector"}])
        if path.endswith("/status/current"):
            return ok({"status": "running", "cpu": 0.05, "mem": 100 * 2**20, "maxmem": 512 * 2**20, "uptime": 5})
        if path.startswith("nodes/pve1/tasks/") and path.endswith("/status"):
            return ok({"status": "stopped", "exitstatus": "OK"})
        return httpx.Response(501, json={"data": None})


@pytest.fixture
def fake():
    tools._pending.clear()
    return Fake()


def api(fake: Fake) -> Proxmox:
    return Proxmox("https://pve.test:8006", "claude@pve!connector", "s", transport=httpx.MockTransport(fake))


def test_needs_a_token(monkeypatch):
    monkeypatch.delenv("PVE_TOKEN_ID", raising=False)
    monkeypatch.delenv("PVE_TOKEN_SECRET", raising=False)
    with pytest.raises(ProxmoxError):
        Proxmox("https://pve.test:8006")


def test_errors_carry_proxmoxs_reason():
    t = httpx.MockTransport(lambda r: httpx.Response(403, extensions={"reason_phrase": b"Permission check failed"}))
    with pytest.raises(ProxmoxError, match="Permission check failed"):
        Proxmox("https://pve.test:8006", "u@pve!t", "s", transport=t).get("nodes")


def test_needs_attention_finds_each_kind_of_problem(fake):
    n = tools.needs_attention(api(fake))
    assert [i.id for i in n.not_in_any_backup_job] == [104]
    assert [i.id for i in n.backups_overdue] == [101] and "9.0 days" in n.backups_overdue[0].detail
    assert [i.id for i in n.production_stopped] == [104]
    assert [i.id for i in n.old_snapshots] == [104]
    assert [i.guest for i in n.storage_filling] == ["tank"]


def test_find_and_resolve_by_name_or_id(fake):
    assert [g.name for g in tools.find_guests(api(fake), tag="web").guests] == ["web-01"]
    assert tools.guest_details(api(fake), "101").guest.name == "erp-db"
    assert "No VM or container" in tools.guest_details(api(fake), "nope").problem


def test_power_sends_nothing_without_a_code(fake):
    c = tools.power_guest(api(fake), "mail-relay", "start")
    assert not c.done and c.confirmation and "mail-relay" in c.summary and fake.posts == []


def test_power_with_the_code_acts_once(fake):
    a = api(fake)
    code = tools.power_guest(a, "erp-db", "reboot").confirmation
    done = tools.power_guest(a, "erp-db", "reboot", code)
    assert done.done and done.guest.status == "running"
    assert fake.posts == [("nodes/pve1/qemu/101/status/reboot", {})]
    assert not tools.power_guest(a, "erp-db", "reboot", code).done  # used once


def test_a_code_only_fits_its_own_request(fake):
    a = api(fake)
    code = tools.power_guest(a, "erp-db", "reboot").confirmation
    assert not tools.power_guest(a, "erp-db", "shutdown", code).done
    assert not tools.snapshot_guest(a, "erp-db", "before-update", confirmation=code).done
    assert fake.posts == []


def test_power_refuses_what_makes_no_sense(fake):
    assert "already running" in tools.power_guest(api(fake), "erp-db", "start").problem
    assert "isn't running" in tools.power_guest(api(fake), "mail-relay", "reboot").problem


def test_snapshot_previews_then_takes_it_with_a_note(fake):
    a = api(fake)
    p = tools.snapshot_guest(a, "web-01", "before-update", "before the nginx update")
    assert not p.done and fake.posts == []
    assert tools.snapshot_guest(a, "web-01", "before-update", "before the nginx update", p.confirmation).done
    path, body = fake.posts[0]
    assert path == "nodes/pve1/lxc/201/snapshot" and body["snapname"] == "before-update"
    assert "via Claude" in body["description"]


def test_snapshot_names_are_checked(fake):
    assert "starts with a letter" in tools.snapshot_guest(api(fake), "web-01", "1 bad name").problem


def test_an_expired_code_is_refused(fake, monkeypatch):
    a = api(fake)
    code = tools.power_guest(a, "erp-db", "reboot").confirmation
    monkeypatch.setattr(tools, "CONFIRM_TTL_S", -1)
    assert not tools.power_guest(a, "erp-db", "reboot", code).done
    assert fake.posts == []
