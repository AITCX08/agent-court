"""Tests for the PR-5 shenpi (审批) layer.

Covers:
- list_pending splits active vs expired
- approve / deny / sweep_expired round-trip
- audit log shape
- 3 channel notifiers: terminal writes event.log, feishu POSTs webhook,
  wechat invokes cc-connect subprocess
- error paths: invalid ids, missing files, io_error fallback
- CLI happy path
- MCP tools list_pending / approve_pending
- End-to-end via daemon: human_required message lands → notify fires
- BangjiaoConfig parses shenpi block correctly
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import bangjiao  # noqa: E402
import shenpi    # noqa: E402
import yiguan_daemon  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _seed(root: Path, project: str, *,
          shenpi_block: dict | None = None,
          allow_paths: list[str] | None = None):
    pdir = root / "projects" / project
    (pdir / "bus").mkdir(parents=True)
    (pdir / "prompts").mkdir(parents=True)
    (pdir / "logs").mkdir(parents=True)
    fed: dict = {"enabled": True, "expose_roles": ["foreman"]}
    if allow_paths is not None:
        fed["allow_paths"] = allow_paths
    if shenpi_block is not None:
        fed["approvals"] = shenpi_block
    cy = {
        "project": project,
        "session": f"court-{project}",
        "attach_window": "foreman",
        "default_cli": "intentionally-missing-cli-for-test-x9z",
        "roles": [{"name": "foreman", "prompt": "foreman.md", "work_dir": "/tmp"}],
        "federation": fed,
    }
    (pdir / "court.yaml").write_text(yaml.safe_dump(cy))
    return pdir


@pytest.fixture
def root_dir(tmp_path, monkeypatch):
    root = tmp_path / "court-root"
    root.mkdir()
    monkeypatch.setenv("COURT_ROOT", str(root))
    monkeypatch.setenv("COURT_HOSTNAME", "testhost")
    return root


def _write_pending(pdir: Path, peer: str, msg_id: str, *,
                   age_seconds: int = 30,
                   body: str = "hello",
                   reasons: list[str] | None = None,
                   msg_from: str = "upstream", msg_to: str = "foreman") -> Path:
    """Create a fake pending-approval file with given age."""
    pa = pdir / "bus" / peer / "pending-approval"
    pa.mkdir(parents=True, exist_ok=True)
    now = int(datetime.now(timezone.utc).timestamp())
    ts = now - age_seconds
    fname = f"{ts}-{msg_id}-{msg_from}-to-{msg_to}.md"
    fpath = pa / fname
    rs = reasons or []
    rs_str = "[" + ", ".join(f"'{r}'" for r in rs) + "]"
    content = (
        f"---\n"
        f"id: {msg_id}\n"
        f"from: {msg_from}\n"
        f"to: {msg_to}\n"
        f"policy_reasons: {rs_str}\n"
        f"---\n\n{body}\n"
    )
    fpath.write_text(content)
    return fpath


# ---------------------------------------------------------------------------
# config parsing
# ---------------------------------------------------------------------------


def test_shenpi_config_defaults_disabled(root_dir):
    _seed(root_dir, "p")
    cfg = bangjiao.load_bangjiao("p").shenpi
    assert cfg.enabled is False
    assert cfg.channels == []
    assert cfg.timeout_seconds == 0


def test_shenpi_config_defaults_terminal_when_enabled_without_channels(root_dir):
    _seed(root_dir, "p", shenpi_block={"enabled": True})
    cfg = bangjiao.load_bangjiao("p").shenpi
    assert cfg.enabled is True
    assert cfg.channels == ["terminal"]


def test_shenpi_config_filters_unknown_channels(root_dir):
    _seed(root_dir, "p", shenpi_block={
        "enabled": True, "channels": ["terminal", "discord", "feishu", "feishu"],
    })
    cfg = bangjiao.load_bangjiao("p").shenpi
    assert cfg.channels == ["terminal", "feishu"]


def test_shenpi_config_clamps_bad_timeout(root_dir):
    _seed(root_dir, "p", shenpi_block={"enabled": True, "timeout_seconds": -42})
    cfg = bangjiao.load_bangjiao("p").shenpi
    assert cfg.timeout_seconds == 0
    _seed(root_dir, "p2", shenpi_block={"enabled": True, "timeout_seconds": "not-a-number"})
    cfg = bangjiao.load_bangjiao("p2").shenpi
    assert cfg.timeout_seconds == 0


def test_shenpi_config_parses_feishu_and_wechat(root_dir):
    _seed(root_dir, "p", shenpi_block={
        "enabled": True,
        "channels": ["terminal", "feishu", "wechat"],
        "timeout_seconds": 3600,
        "feishu": {"webhook_url": "https://hook.example/abc", "mention": ["uid-1"]},
        "wechat": {
            "cc_connect_project": "k2work",
            "cc_connect_session_key": "session-xyz",
        },
    })
    cfg = bangjiao.load_bangjiao("p").shenpi
    assert cfg.timeout_seconds == 3600
    assert cfg.feishu.webhook_url == "https://hook.example/abc"
    assert cfg.feishu.mention == ["uid-1"]
    assert cfg.wechat.cc_connect_project == "k2work"
    assert cfg.wechat.cc_connect_session_key == "session-xyz"


# ---------------------------------------------------------------------------
# list_pending / find_pending
# ---------------------------------------------------------------------------


def test_list_pending_empty(root_dir):
    _seed(root_dir, "p")
    listing = shenpi.list_pending("p")
    assert listing == {"pending": [], "expired": []}


def test_list_pending_parses_files(root_dir):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "aaaa111a", body="hi from bob",
                   reasons=["sensitive keyword 'password' in body"])
    listing = shenpi.list_pending("p")
    assert len(listing["pending"]) == 1
    item = listing["pending"][0]
    assert item.msg_id == "aaaa111a"
    assert item.peer == "bob"
    assert item.body.strip() == "hi from bob"
    assert "sensitive keyword 'password' in body" in item.reasons


def test_list_pending_splits_expired_when_timeout(root_dir):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "fffe5111", age_seconds=10)
    _write_pending(pdir, "bob", "01da1234", age_seconds=10_000)
    listing = shenpi.list_pending("p", timeout_seconds=300)
    assert [i.msg_id for i in listing["pending"]] == ["fffe5111"]
    assert [i.msg_id for i in listing["expired"]] == ["01da1234"]


def test_find_pending_rejects_unsafe_id(root_dir):
    _seed(root_dir, "p")
    assert shenpi.find_pending("p", "../../etc/passwd") is None


def test_find_pending_returns_none_for_missing(root_dir):
    _seed(root_dir, "p")
    assert shenpi.find_pending("p", "deadbeef") is None


def test_list_pending_path_traversal_rejected(root_dir):
    with pytest.raises(ValueError):
        shenpi.list_pending("../shared")


# ---------------------------------------------------------------------------
# approve / deny / sweep
# ---------------------------------------------------------------------------


def test_approve_moves_file_to_inbox(root_dir):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "aaaa111a")
    assert shenpi.approve("p", "aaaa111a", by="alice") == "approved"
    inbox = pdir / "bus" / "bob" / "inbox"
    assert any(f.name.endswith("aaaa111a-upstream-to-foreman.md")
               for f in inbox.glob("*.md"))
    # original pending file is gone
    pa = pdir / "bus" / "bob" / "pending-approval"
    assert list(pa.glob("*.md")) == []


def test_deny_moves_file_to_denied(root_dir):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "aaaa111a")
    assert shenpi.deny("p", "aaaa111a") == "denied"
    denied = pdir / "bus" / "bob" / "denied"
    assert any(f.name.endswith("aaaa111a-upstream-to-foreman.md")
               for f in denied.glob("*.md"))


def test_approve_not_found(root_dir):
    _seed(root_dir, "p")
    assert shenpi.approve("p", "deadbeef") == "not_found"
    assert shenpi.deny("p", "deadbeef") == "not_found"


def test_approve_refuses_expired(root_dir):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "01da1234", age_seconds=10_000)
    assert shenpi.approve("p", "01da1234", timeout_seconds=300) == "expired"
    # File still on disk, didn't move.
    pa = pdir / "bus" / "bob" / "pending-approval"
    assert len(list(pa.glob("*.md"))) == 1


def test_deny_works_on_expired(root_dir):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "01da1234", age_seconds=10_000)
    assert shenpi.deny("p", "01da1234") == "denied"


def test_sweep_expired_moves_old_to_denied(root_dir):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "fffe5111", age_seconds=10)
    _write_pending(pdir, "bob", "01da1234", age_seconds=10_000)
    _write_pending(pdir, "bob", "01da2222", age_seconds=10_001)
    result = shenpi.sweep_expired("p", timeout_seconds=300)
    assert set(result["swept"]) == {"01da1234", "01da2222"}
    pa = pdir / "bus" / "bob" / "pending-approval"
    assert [f.name for f in pa.glob("*.md")][0].endswith("fffe5111-upstream-to-foreman.md")


def test_sweep_noop_when_timeout_zero(root_dir):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "01da1234", age_seconds=10_000)
    assert shenpi.sweep_expired("p", timeout_seconds=0) == {"swept": []}


def test_audit_log_records_approval(root_dir):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "aaaa111a")
    shenpi.approve("p", "aaaa111a", by="alice@laptop")
    log = pdir / "logs" / "approval-log.jsonl"
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    actions = [l["action"] for l in lines]
    assert "approved" in actions
    assert any(l["by"] == "alice@laptop" for l in lines)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def _make_item(pdir: Path, peer: str = "bob", msg_id: str = "aaaa111a") -> shenpi.PendingItem:
    fpath = _write_pending(pdir, peer, msg_id, reasons=["test reason"])
    return shenpi._parse_file("p", peer, fpath)


def test_terminal_channel_appends_event_log(root_dir):
    pdir = _seed(root_dir, "p")
    item = _make_item(pdir)
    cfg = bangjiao.ShenpiConfig(enabled=True, channels=["terminal"])
    asyncio.run(shenpi.notify(item, shenpi_cfg=cfg))
    log = pdir / "shared" / "event.log"
    assert log.is_file()
    assert "shenpi/留中" in log.read_text()
    assert "aaaa111a" in log.read_text()


def test_feishu_channel_posts_webhook(root_dir):
    pdir = _seed(root_dir, "p")
    item = _make_item(pdir)
    cfg = bangjiao.ShenpiConfig(
        enabled=True, channels=["feishu"],
        feishu=bangjiao.FeishuChannelConfig(webhook_url="https://hook.example/abc"),
    )
    captured = {}

    class _FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = req.data.decode()
        return _FakeResp()

    with patch("urllib.request.urlopen", _fake_urlopen):
        asyncio.run(shenpi.notify(item, shenpi_cfg=cfg))

    assert captured["url"] == "https://hook.example/abc"
    payload = json.loads(captured["body"])
    assert payload["msg_type"] == "text"
    assert "aaaa111a" in payload["content"]["text"]
    assert "court-approve p approve aaaa111a" in payload["content"]["text"]


def test_feishu_channel_without_webhook_records_failure(root_dir):
    pdir = _seed(root_dir, "p")
    item = _make_item(pdir)
    cfg = bangjiao.ShenpiConfig(
        enabled=True, channels=["feishu"],
        feishu=bangjiao.FeishuChannelConfig(webhook_url=None),
    )
    out = asyncio.run(shenpi.notify(item, shenpi_cfg=cfg))
    assert "error" in out["feishu"]
    log = pdir / "logs" / "approval-log.jsonl"
    assert "notify_failed" in log.read_text()


def test_wechat_channel_invokes_cc_connect_send(root_dir):
    pdir = _seed(root_dir, "p")
    item = _make_item(pdir)
    cfg = bangjiao.ShenpiConfig(
        enabled=True, channels=["wechat"],
        wechat=bangjiao.WechatChannelConfig(
            cc_connect_bin="cc-connect",
            cc_connect_project="k2work",
            cc_connect_session_key="sess-abc",
        ),
    )
    captured_calls = []

    async def _fake_exec(*args, env=None, **kwargs):
        captured_calls.append({"args": args, "env": env})
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        proc.returncode = 0
        return proc

    with patch("shutil.which", return_value="/usr/local/bin/cc-connect"), \
         patch("asyncio.create_subprocess_exec", _fake_exec):
        out = asyncio.run(shenpi.notify(item, shenpi_cfg=cfg))
    assert out["wechat"] == "ok"
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["args"][:2] == ("/usr/local/bin/cc-connect", "send")
    # Message content carries the msg_id + approve instruction.
    msg_pos = call["args"].index("--message")
    assert "aaaa111a" in call["args"][msg_pos + 1]
    assert call["env"]["CC_PROJECT"] == "k2work"
    assert call["env"]["CC_SESSION_KEY"] == "sess-abc"


def test_wechat_channel_session_key_optional(root_dir):
    """Empty session_key is fine — cc-connect picks the first active session."""
    pdir = _seed(root_dir, "p")
    item = _make_item(pdir)
    cfg = bangjiao.ShenpiConfig(
        enabled=True, channels=["wechat"],
        wechat=bangjiao.WechatChannelConfig(
            cc_connect_bin="cc-connect",
            cc_connect_project="k2work",
            cc_connect_session_key="",   # left blank
        ),
    )
    captured = []

    async def _fake_exec(*args, env=None, **kwargs):
        captured.append({"args": args, "env": env})
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        proc.returncode = 0
        return proc

    with patch("shutil.which", return_value="/usr/local/bin/cc-connect"), \
         patch("asyncio.create_subprocess_exec", _fake_exec):
        out = asyncio.run(shenpi.notify(item, shenpi_cfg=cfg))
    assert out["wechat"] == "ok"
    assert "--session" not in captured[0]["args"]
    assert "CC_SESSION_KEY" not in captured[0]["env"]


def test_wechat_channel_missing_binary_recorded_as_failure(root_dir):
    pdir = _seed(root_dir, "p")
    item = _make_item(pdir)
    cfg = bangjiao.ShenpiConfig(
        enabled=True, channels=["wechat"],
        wechat=bangjiao.WechatChannelConfig(
            cc_connect_project="k2work", cc_connect_session_key="x",
        ),
    )
    with patch("shutil.which", return_value=None):
        out = asyncio.run(shenpi.notify(item, shenpi_cfg=cfg))
    assert "error" in out["wechat"]


def test_one_channel_failure_does_not_stop_others(root_dir):
    pdir = _seed(root_dir, "p")
    item = _make_item(pdir)
    cfg = bangjiao.ShenpiConfig(
        enabled=True, channels=["terminal", "feishu"],
        feishu=bangjiao.FeishuChannelConfig(webhook_url=None),  # will fail
    )
    out = asyncio.run(shenpi.notify(item, shenpi_cfg=cfg))
    assert out["terminal"] == "ok"
    assert "error" in out["feishu"]
    # Terminal channel still wrote the event.log line.
    assert "aaaa111a" in (pdir / "shared" / "event.log").read_text()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_list_empty(root_dir, capsys):
    _seed(root_dir, "p")
    import shenpi_cli
    rc = shenpi_cli.main(["p", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no pending items" in out


def test_cli_list_with_items(root_dir, capsys):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "aaaa111a", reasons=["reason A"])
    import shenpi_cli
    rc = shenpi_cli.main(["p", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "aaaa111a" in out
    assert "bob" in out


def test_cli_approve_happy(root_dir, capsys):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "aaaa111a")
    import shenpi_cli
    rc = shenpi_cli.main(["p", "approve", "aaaa111a"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "approved" in out


def test_cli_deny_happy(root_dir, capsys):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "aaaa111a")
    import shenpi_cli
    rc = shenpi_cli.main(["p", "deny", "aaaa111a"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "denied" in out


def test_cli_cleanup_sweeps_expired(root_dir, capsys):
    pdir = _seed(root_dir, "p", shenpi_block={"enabled": True, "timeout_seconds": 300})
    _write_pending(pdir, "bob", "01da1234", age_seconds=10_000)
    _write_pending(pdir, "bob", "fffe5111", age_seconds=10)
    import shenpi_cli
    rc = shenpi_cli.main(["p", "cleanup"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "01da1234" in out
    # fresh stayed
    pa = pdir / "bus" / "bob" / "pending-approval"
    fresh = list(pa.glob("*.md"))
    assert len(fresh) == 1
    assert "fffe5111" in fresh[0].name


def test_cli_cleanup_noop_without_timeout(root_dir, capsys):
    pdir = _seed(root_dir, "p")  # timeout_seconds defaults to 0
    _write_pending(pdir, "bob", "01da1234", age_seconds=10_000)
    import shenpi_cli
    rc = shenpi_cli.main(["p", "cleanup"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no-op" in out


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def test_mcp_list_pending_empty(root_dir):
    _seed(root_dir, "p")
    from server import list_pending
    out = list_pending("p")
    assert out["pending"] == []
    assert out["expired"] == []


def test_mcp_list_pending_returns_items(root_dir):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "aaaa111a", body="hi", reasons=["r1"])
    from server import list_pending
    out = list_pending("p")
    assert len(out["pending"]) == 1
    row = out["pending"][0]
    assert row["msg_id"] == "aaaa111a"
    assert row["peer"] == "bob"
    assert row["reasons"] == ["r1"]
    assert row["body_excerpt"] == "hi"


def test_mcp_approve_pending_approve(root_dir):
    pdir = _seed(root_dir, "p")
    _write_pending(pdir, "bob", "aaaa111a")
    from server import approve_pending
    out = approve_pending("p", "aaaa111a", "approve", by="wechat-user-bob")
    assert out == {"ok": True, "result": "approved", "project": "p", "msg_id": "aaaa111a"}


def test_mcp_approve_pending_invalid_action(root_dir):
    _seed(root_dir, "p")
    from server import approve_pending
    out = approve_pending("p", "aaaa111a", "magic")
    assert out["error"] == "invalid_action"


def test_mcp_approve_pending_unknown_project():
    from server import approve_pending
    out = approve_pending("nonexistent-xyz", "aaaa111a", "approve")
    assert out["error"] == "unknown_project"


# ---------------------------------------------------------------------------
# E2E via daemon
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_shenpi(root_dir):
    """A project where:
    - Bob is tier_a (forces human_required)
    - shenpi.enabled = True with terminal channel only (no network)
    - Bob's keypair is this project's own keypair (so daemon can verify)
    """
    import bangjiao as bj
    _seed(root_dir, "p",
          shenpi_block={"enabled": True, "channels": ["terminal"]},
          allow_paths=["bus/foreman/inbox/**"])
    identity = bj.generate_keypair("p", force=True)
    bj.project_peers_yaml_path("p").write_text(yaml.safe_dump({
        "peers": [{
            "name": "Bob",
            "court_id": "bob",
            "url": "http://127.0.0.1:0",
            "pub_key_fingerprint": identity.fingerprint,
            "pub_key_b64": identity.pub_b64,
            "relation": "sibling",
            "policy_tier": "tier_a",  # human_required
        }],
    }))
    return identity


def _signed(identity, *, attaches=None, body="hi"):
    import secrets
    import bangjiao as bj
    msg = {
        "from": "upstream",
        "from_court": "bob",
        "to": "foreman",
        "body": body,
        "ts": bj.iso_now(),
        "id": secrets.token_hex(4),
    }
    if attaches:
        msg["attaches"] = list(attaches)
    msg["signature"] = bj.sign_message(msg, identity.priv)
    return msg


async def _post(project, payload):
    import aiohttp
    from aiohttp.test_utils import TestServer
    app = yiguan_daemon.make_app(project)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://127.0.0.1:{server.port}/inbox"
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload) as r:
                return r.status, await r.json()
    finally:
        await server.close()


def test_e2e_human_required_fires_terminal_notify(project_with_shenpi, root_dir):
    """Tier_a peer → human_required → terminal channel writes event.log."""
    identity = project_with_shenpi
    msg = _signed(identity, attaches=["bus/foreman/inbox/x.md"])
    status, body = asyncio.run(_post("p", msg))
    assert body["decision"] == "human_required"

    # Give the fire-and-forget task a moment to flush.
    asyncio.run(asyncio.sleep(0.1))

    log = root_dir / "projects" / "p" / "shared" / "event.log"
    assert log.is_file()
    text = log.read_text()
    assert "shenpi/留中" in text


def test_e2e_auto_pass_does_not_trigger_notify(root_dir):
    """Tier_c peer → auto_pass → shenpi.notify must NOT fire."""
    import bangjiao as bj
    _seed(root_dir, "p",
          shenpi_block={"enabled": True, "channels": ["terminal"]},
          allow_paths=["bus/foreman/inbox/**"])
    identity = bj.generate_keypair("p", force=True)
    bj.project_peers_yaml_path("p").write_text(yaml.safe_dump({
        "peers": [{
            "name": "Bob", "court_id": "bob",
            "url": "http://127.0.0.1:0",
            "pub_key_fingerprint": identity.fingerprint,
            "pub_key_b64": identity.pub_b64,
            "relation": "sibling",
            "policy_tier": "tier_c",  # auto_pass
        }],
    }))
    msg = _signed(identity, attaches=["bus/foreman/inbox/x.md"])
    status, body = asyncio.run(_post("p", msg))
    assert body["decision"] == "auto_pass"

    log = root_dir / "projects" / "p" / "shared" / "event.log"
    # The daemon's own _log doesn't write to shared/event.log (that's the
    # watcher's responsibility). If our notify channel wrote anything,
    # it would mention shenpi/留中. Since auto_pass doesn't trigger
    # shenpi, the file either doesn't exist or doesn't contain it.
    if log.is_file():
        assert "shenpi/留中" not in log.read_text()
