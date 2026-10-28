"""Against the local Proxmox VE with the SAMPLE estate (scripts/reset.sh). Skipped when it isn't running.

Run: uv run --env-file .env pytest tests/test_live.py
"""

import os
import time

import httpx
import pytest

from proxmox_mcp import tools
from proxmox_mcp.client import Proxmox, ProxmoxError


def _up() -> bool:
    # with the token: Proxmox holds back an unauthenticated answer for a few seconds
    try:
        return bool(os.environ.get("PVE_TOKEN_SECRET")) and bool(Proxmox().get("version"))
    except (httpx.HTTPError, ProxmoxError):
        return False


pytestmark = pytest.mark.skipif(not _up(), reason="local Proxmox VE not running (scripts/up.sh)")


@pytest.fixture(scope="module")
def api():
    return Proxmox()


def test_the_sample_estate_needs_attention(api):
    n = tools.needs_attention(api)
    assert {i.id for i in n.not_in_any_backup_job} == {103, 104, 105, 204}
    assert [i.id for i in n.backups_overdue] == [102]
    assert [i.id for i in n.production_stopped] == [104]
    assert [i.id for i in n.old_snapshots] == [105]


def test_details_come_with_snapshots_and_the_last_backup(api):
    d = tools.guest_details(api, "erp-db")
    assert d.guest.id == 101 and d.last_backup and any(s.name == "before-upgrade" for s in d.snapshots)


def test_a_snapshot_lands_in_proxmox(api):
    name = f"live-test-{int(time.time())}"  # a new name each run; scripts/reset.sh clears them
    p = tools.snapshot_guest(api, "web-02", name, "test run")
    assert tools.snapshot_guest(api, "web-02", name, "test run", p.confirmation).done
    d = tools.guest_details(api, "web-02")
    assert any(s.name == name for s in d.snapshots)
    assert d.recent_tasks[0].by == os.environ["PVE_TOKEN_ID"].split("!")[0]  # the task log names the token's user
