"""Tests for the PR-4 grants layer.

Covers:
- mint / load / revoke round-trip
- TTL parser edge cases
- expired grants are filtered from load_active_grants
- grants are peer-scoped (peer A's grant doesn't widen peer B)
- grants widen allow_paths inside lvli.evaluate
- grants do NOT bypass HARDCODED_DENY_PATHS or user deny_paths
- grants do NOT relax expose_roles or change tier action
- end-to-end HTTP: a path covered by a grant lets the message through
- revoke takes effect immediately
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import lingpai  # noqa: E402
import yiguan_daemon  # noqa: E402
import bangjiao  # noqa: E402
import lvli  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed(root: Path, project: str, *,
          allow_paths: list[str] | None = None,
          deny_paths: list[str] | None = None) -> Path:
    pdir = root / "projects" / project
    (pdir / "bus").mkdir(parents=True)
    (pdir / "prompts").mkdir(parents=True)

    fed = {
        "enabled": True,
        "expose_roles": ["zongguan"],
    }
    if allow_paths is not None:
        fed["allow_paths"] = allow_paths
    if deny_paths is not None:
        fed["deny_paths"] = deny_paths

    court_yaml = {
        "project": project,
        "session": f"court-{project}",
        "attach_window": "zongguan",
        "default_cli": "intentionally-missing-cli-for-test-x9z",
        "roles": [{"name": "zongguan", "prompt": "zongguan.md", "work_dir": "/tmp"}],
        "bangjiao": fed,
    }
    (pdir / "yamen.yaml").write_text(yaml.safe_dump(court_yaml))
    return pdir


@pytest.fixture
def root_dir(tmp_path, monkeypatch):
    root = tmp_path / "court-root"
    root.mkdir()
    monkeypatch.setenv("YAMEN_ROOT", str(root))
    monkeypatch.setenv("COURT_HOSTNAME", "testhost")
    return root


# ---------------------------------------------------------------------------
# TTL parsing
# ---------------------------------------------------------------------------


def test_parse_ttl_seconds_int():
    assert lingpai.parse_ttl(30) == 30


def test_parse_ttl_minutes():
    assert lingpai.parse_ttl("30m") == 1800


def test_parse_ttl_hours():
    assert lingpai.parse_ttl("2h") == 7200


def test_parse_ttl_compound():
    assert lingpai.parse_ttl("2h30m") == 9000
    assert lingpai.parse_ttl("1d12h") == 86400 + 12 * 3600
    assert lingpai.parse_ttl("1d 6h") == 86400 + 6 * 3600


def test_parse_ttl_seconds_string():
    assert lingpai.parse_ttl("90") == 90
    assert lingpai.parse_ttl("45s") == 45


def test_parse_ttl_case_insensitive():
    assert lingpai.parse_ttl("30M") == 1800
    assert lingpai.parse_ttl("1H") == 3600


def test_parse_ttl_rejects_garbage():
    with pytest.raises(ValueError):
        lingpai.parse_ttl("forever")
    with pytest.raises(ValueError):
        lingpai.parse_ttl("")
    with pytest.raises(ValueError):
        lingpai.parse_ttl(0)
    with pytest.raises(ValueError):
        lingpai.parse_ttl("-30m")


# ---------------------------------------------------------------------------
# Mint + load + revoke
# ---------------------------------------------------------------------------


def test_mint_writes_file_with_expected_shape(root_dir):
    _seed(root_dir, "p")
    g = lingpai.mint_grant(
        "p", "bob", ["notes/x.md", "notes/y.md"], ttl="1h", issued_by="alice@host",
    )
    path = lingpai.grants_dir("p") / f"{g.id}.json"
    assert path.is_file()
    raw = json.loads(path.read_text())
    assert raw["id"] == g.id
    assert raw["granted_to"] == "bob"
    assert raw["paths"] == ["notes/x.md", "notes/y.md"]
    assert raw["issued_by"] == "alice@host"
    # issued_ts < expires_ts and roughly 1h apart
    issued = datetime.fromisoformat(raw["issued_ts"])
    expires = datetime.fromisoformat(raw["expires_ts"])
    delta = (expires - issued).total_seconds()
    assert 3590 <= delta <= 3610


def test_mint_rejects_empty_paths(root_dir):
    _seed(root_dir, "p")
    with pytest.raises(ValueError):
        lingpai.mint_grant("p", "bob", [], ttl="30m")


def test_mint_rejects_non_string_path(root_dir):
    _seed(root_dir, "p")
    with pytest.raises(ValueError):
        lingpai.mint_grant("p", "bob", ["ok.md", 42], ttl="30m")


def test_mint_rejects_hostile_peer_name(root_dir):
    _seed(root_dir, "p")
    with pytest.raises(bangjiao.UnsafeNameError):
        lingpai.mint_grant("p", "../shared", ["x.md"], ttl="30m")


def test_list_grants_returns_sorted_by_issue_time(root_dir):
    _seed(root_dir, "p")
    g1 = lingpai.mint_grant("p", "a", ["x.md"], ttl="1h")
    time.sleep(1.1)  # ensure issued_ts differs
    g2 = lingpai.mint_grant("p", "b", ["y.md"], ttl="1h")
    rows = lingpai.list_grants("p")
    assert [g.id for g in rows] == [g1.id, g2.id]


def test_revoke_removes_file(root_dir):
    _seed(root_dir, "p")
    g = lingpai.mint_grant("p", "bob", ["x.md"], ttl="30m")
    assert lingpai.revoke_grant("p", g.id) == "revoked"
    assert lingpai.revoke_grant("p", g.id) == "not_found"  # idempotent
    assert lingpai.list_grants("p") == []


def test_revoke_rejects_unsafe_id(root_dir):
    _seed(root_dir, "p")
    assert lingpai.revoke_grant("p", "../../etc") == "invalid_id"


def test_load_active_grants_filters_expired(root_dir):
    _seed(root_dir, "p")
    g_live = lingpai.mint_grant("p", "alive", ["x.md"], ttl="1h")
    g_dead = lingpai.mint_grant("p", "dead",  ["y.md"], ttl="1h")
    # Manually backdate the second grant.
    p = lingpai.grants_dir("p") / f"{g_dead.id}.json"
    raw = json.loads(p.read_text())
    raw["expires_ts"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    p.write_text(json.dumps(raw))

    active = lingpai.load_active_grants("p")
    assert [g.id for g in active] == [g_live.id]


def test_load_grants_for_peer_isolates(root_dir):
    _seed(root_dir, "p")
    lingpai.mint_grant("p", "alice", ["alice/**"], ttl="1h")
    lingpai.mint_grant("p", "bob",   ["bob/**"],   ttl="1h")
    assert lingpai.load_grants_for_peer("p", "alice") == ["alice/**"]
    assert lingpai.load_grants_for_peer("p", "bob")   == ["bob/**"]
    assert lingpai.load_grants_for_peer("p", "ghost") == []


# ---------------------------------------------------------------------------
# lvli.evaluate integration
# ---------------------------------------------------------------------------


def _msg(**overrides):
    base = {"from_court": "bob", "to": "zongguan", "body": "ok", "id": "x"}
    base.update(overrides)
    return base


def test_grant_widens_allow_paths():
    """A path NOT in allow_paths but covered by grant_paths must pass
    instead of being upgraded to human_required."""
    msg = _msg(attaches=["notes/secret.md"])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=["bus/zongguan/inbox/**"],
        deny_paths=[],
        grant_paths=["notes/**"],
    )
    assert d.action == "auto_pass"
    assert any("active grant" in r for r in d.reasons)


def test_grant_does_not_bypass_hardcoded_deny():
    msg = _msg(attaches=[".ssh/id_rsa"])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=["bus/zongguan/inbox/**"],
        deny_paths=[],
        grant_paths=[".ssh/**"],         # peer "granted" ssh — must still deny
    )
    assert d.action == "denied"


def test_grant_does_not_bypass_user_deny():
    msg = _msg(attaches=["prompts/zongguan.md"])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=["bus/zongguan/inbox/**", "prompts/**"],
        deny_paths=["prompts/**"],         # deny wins
        grant_paths=["prompts/**"],
    )
    assert d.action == "denied"


def test_empty_grants_behave_like_no_grants():
    msg = _msg(attaches=["bus/zongguan/inbox/x.md"])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=["bus/zongguan/inbox/**"],
        deny_paths=[],
        grant_paths=[],
    )
    assert d.action == "auto_pass"


def test_grant_cannot_invent_allow_paths_when_none_exist():
    """If allow_paths is empty (no static whitelist), a grant alone
    does NOT start enforcing a whitelist — the policy still falls
    through to the tier check. This matches the documented semantics:
    grants are a *widening*, not a replacement."""
    msg = _msg(attaches=["random.md"])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=[],                 # no static whitelist
        deny_paths=[],
        grant_paths=["only-this.md"],
    )
    # No allow_paths to enforce, so the grant is irrelevant.
    assert d.action == "auto_pass"


# ---------------------------------------------------------------------------
# End-to-end through the daemon
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_self_peer(root_dir):
    """Project with allow_paths=['bus/zongguan/inbox/**'] and a peer 'bob'
    whose pubkey is this project's own keypair (so test can sign and
    daemon can verify in one process)."""
    _seed(root_dir, "p", allow_paths=["bus/zongguan/inbox/**"])
    identity = bangjiao.generate_keypair("p", force=True)
    bangjiao.project_peers_yaml_path("p").write_text(yaml.safe_dump({
        "peers": [{
            "name": "Bob",
            "yamen_id": "bob",
            "url": "http://127.0.0.1:0",
            "pub_key_fingerprint": identity.fingerprint,
            "pub_key_b64": identity.pub_b64,
            "relation": "child",
            "policy_tier": "tier_c",   # auto_pass when allow_paths OK
        }],
    }))
    return identity


def _signed(identity, *, attaches=None, body="hi"):
    import secrets
    msg = {
        "from": "upstream",
        "from_court": "bob",
        "to": "zongguan",
        "body": body,
        "ts": bangjiao.iso_now(),
        "id": secrets.token_hex(4),
    }
    if attaches:
        msg["attaches"] = list(attaches)
    msg["signature"] = bangjiao.sign_message(msg, identity.priv)
    return msg


async def _post(project, payload):
    """Fresh app per call — aiohttp Application can't be reused across loops."""
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


def _round_trip(project, payload):
    return asyncio.run(_post(project, payload))


def test_e2e_grant_lets_otherwise_blocked_attach_through(project_with_self_peer):
    """Without a grant: a notes/* attach is outside allow_paths and ends
    up in pending-approval. With a grant: same message → auto_pass."""
    identity = project_with_self_peer

    # Pre-grant: blocked
    msg = _signed(identity, attaches=["notes/q2.md"])
    status, body = _round_trip("p", msg)
    assert body["decision"] == "human_required"

    # Grant access; new message should sail through
    lingpai.mint_grant("p", "bob", ["notes/**"], ttl="1h")
    msg2 = _signed(identity, attaches=["notes/q2.md"])
    status2, body2 = _round_trip("p", msg2)
    assert status2 == 200
    assert body2["decision"] == "auto_pass"
    assert any("active grant" in r for r in body2["reasons"])


def test_e2e_revoke_takes_effect_immediately(project_with_self_peer):
    identity = project_with_self_peer

    g = lingpai.mint_grant("p", "bob", ["notes/**"], ttl="1h")
    # Sanity: grant covers a message
    msg = _signed(identity, attaches=["notes/x.md"])
    _, body = _round_trip("p", msg)
    assert body["decision"] == "auto_pass"

    # Revoke and try again with a fresh id (replay cache would otherwise reject)
    assert lingpai.revoke_grant("p", g.id) == "revoked"
    msg2 = _signed(identity, attaches=["notes/x.md"])
    _, body2 = _round_trip("p", msg2)
    assert body2["decision"] == "human_required"


def test_e2e_grant_for_other_peer_does_not_help(project_with_self_peer):
    """Bob has no grant. Carol does. Bob's attach must still be blocked."""
    identity = project_with_self_peer
    lingpai.mint_grant("p", "carol", ["notes/**"], ttl="1h")

    msg = _signed(identity, attaches=["notes/x.md"])
    _, body = _round_trip("p", msg)
    assert body["decision"] == "human_required"


def test_e2e_grant_still_respects_hardcoded_deny(project_with_self_peer):
    identity = project_with_self_peer
    # Carelessly grant ssh access — hardcoded layer should still bite.
    lingpai.mint_grant("p", "bob", [".ssh/**"], ttl="1h")
    msg = _signed(identity, attaches=[".ssh/id_rsa"])
    _, body = _round_trip("p", msg)
    assert body["decision"] == "denied"


# ---------------------------------------------------------------------------
# PR-4.1 — path containment (Critical from review)
# ---------------------------------------------------------------------------


def test_project_traversal_rejected_in_mint(root_dir):
    """``project="../foo"`` must not let mint write outside YAMEN_ROOT/projects."""
    with pytest.raises(ValueError):
        lingpai.mint_grant("../shared", "bob", ["x.md"], ttl="30m")


def test_project_traversal_rejected_in_list(root_dir):
    with pytest.raises(ValueError):
        lingpai.list_grants("../shared")


def test_project_traversal_rejected_in_revoke(root_dir):
    with pytest.raises(ValueError):
        lingpai.revoke_grant("../shared", "deadbeef")


def test_project_traversal_rejected_in_find(root_dir):
    with pytest.raises(ValueError):
        lingpai.find_grant("../shared", "deadbeef")


def test_project_unsafe_component_rejected(root_dir):
    with pytest.raises(ValueError):
        lingpai.mint_grant("foo/bar", "bob", ["x.md"], ttl="30m")


# ---------------------------------------------------------------------------
# PR-4.1 — TTL bounds (Warning from review)
# ---------------------------------------------------------------------------


def test_parse_ttl_rejects_overflow():
    with pytest.raises(ValueError):
        lingpai.parse_ttl(10**12)
    with pytest.raises(ValueError):
        lingpai.parse_ttl(f"{10**9}d")


def test_parse_ttl_accepts_max():
    assert lingpai.parse_ttl(lingpai.MAX_TTL_SECONDS) == lingpai.MAX_TTL_SECONDS


def test_mint_with_huge_ttl_raises_value_error(root_dir):
    _seed(root_dir, "p")
    with pytest.raises(ValueError):
        lingpai.mint_grant("p", "bob", ["x.md"], ttl="9999d")


# ---------------------------------------------------------------------------
# PR-4.1 — atomic write + corrupted JSON (Warning + Info from review)
# ---------------------------------------------------------------------------


def test_atomic_write_no_dotfiles_match_glob(root_dir):
    """Temp files used by atomic-write must not appear in list_lingpai."""
    _seed(root_dir, "p")
    lingpai.mint_grant("p", "bob", ["x.md"], ttl="30m")
    # Drop a tempfile-style stray that should be ignored.
    stray = lingpai.grants_dir("p") / ".some-write-in-progress.json"
    stray.write_text("{half written")
    rows = lingpai.list_grants("p")
    assert len(rows) == 1


def test_corrupted_json_skipped_and_logged(root_dir):
    _seed(root_dir, "p")
    g = lingpai.mint_grant("p", "bob", ["x.md"], ttl="30m")
    # Append garbage to the file → JSON parse error.
    p = lingpai.grants_dir("p") / f"{g.id}.json"
    p.write_text("{ not json")
    rows = lingpai.list_grants("p")
    assert rows == []
    log = bangjiao.project_peer_errors_log("p")
    assert log.is_file()
    assert "schema mismatch" in log.read_text() or "unparseable" in log.read_text()


def test_oversize_grant_file_skipped(root_dir):
    _seed(root_dir, "p")
    p = lingpai.grants_dir("p")
    p.mkdir(parents=True, exist_ok=True)
    huge = p / "huge.json"
    huge.write_text("x" * (lingpai.MAX_GRANT_FILE_BYTES + 100))
    assert lingpai.list_grants("p") == []


def test_strict_schema_rejects_non_string_paths(root_dir):
    _seed(root_dir, "p")
    g = lingpai.mint_grant("p", "bob", ["x.md"], ttl="30m")
    p = lingpai.grants_dir("p") / f"{g.id}.json"
    raw = json.loads(p.read_text())
    raw["paths"] = [1, 2, 3]
    p.write_text(json.dumps(raw))
    assert lingpai.list_grants("p") == []


def test_strict_schema_rejects_bad_grant_type(root_dir):
    _seed(root_dir, "p")
    g = lingpai.mint_grant("p", "bob", ["x.md"], ttl="30m")
    p = lingpai.grants_dir("p") / f"{g.id}.json"
    raw = json.loads(p.read_text())
    raw["grant_type"] = "wizard"
    p.write_text(json.dumps(raw))
    assert lingpai.list_grants("p") == []


# ---------------------------------------------------------------------------
# PR-4.1 — tier_grants
# ---------------------------------------------------------------------------


def test_mint_tier_grant_roundtrip(root_dir):
    _seed(root_dir, "p")
    g = lingpai.mint_tier_grant("p", "bob", "tier_c", ttl="1h", consume_on_use=True)
    assert g.grant_type == "tier"
    assert g.target_tier == "tier_c"
    assert g.consume_on_use is True
    assert g.paths == []
    # Persisted shape
    p = lingpai.grants_dir("p") / f"{g.id}.json"
    raw = json.loads(p.read_text())
    assert raw["grant_type"] == "tier"
    assert raw["target_tier"] == "tier_c"
    assert raw["consume_on_use"] is True


def test_mint_tier_grant_rejects_bad_tier(root_dir):
    _seed(root_dir, "p")
    with pytest.raises(ValueError):
        lingpai.mint_tier_grant("p", "bob", "tier_z", ttl="1h")


def test_tier_grant_upgrades_peer_in_policy_eval():
    g = lingpai.Grant(
        id="tg1", granted_to="bob",
        issued_ts="2026-01-01T00:00:00+08:00",
        expires_ts="2099-01-01T00:00:00+08:00",
        grant_type="tier", target_tier="tier_c",
    )
    msg = _msg(attaches=[])
    # peer_tier=tier_a normally → human_required. Grant upgrades to tier_c.
    d = lvli.evaluate(
        msg, peer_tier="tier_a", policy=lvli.PolicyConfig(),
        allow_paths=[], deny_paths=[],
        tier_grant=g,
    )
    assert d.action == "auto_pass"
    assert d.tier == "tier_c"
    assert "tg1" in d.grant_hits


def test_tier_grant_does_not_downgrade():
    """A tier_a target grant on a tier_c peer must NOT lower the tier."""
    g = lingpai.Grant(
        id="tg1", granted_to="bob",
        issued_ts="2026-01-01T00:00:00+08:00",
        expires_ts="2099-01-01T00:00:00+08:00",
        grant_type="tier", target_tier="tier_a",
    )
    d = lvli.evaluate(
        _msg(attaches=[]), peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=[], deny_paths=[],
        tier_grant=g,
    )
    assert d.action == "auto_pass"          # tier_c retained
    assert d.tier == "tier_c"
    assert d.grant_hits == []               # grant did not fire


def test_tier_grant_does_not_bypass_hardcoded_deny():
    g = lingpai.Grant(
        id="tg1", granted_to="bob",
        issued_ts="2026-01-01T00:00:00+08:00",
        expires_ts="2099-01-01T00:00:00+08:00",
        grant_type="tier", target_tier="tier_c",
    )
    d = lvli.evaluate(
        _msg(attaches=[".ssh/id_rsa"]),
        peer_tier="tier_a", policy=lvli.PolicyConfig(),
        allow_paths=[], deny_paths=[],
        tier_grant=g,
    )
    assert d.action == "denied"


def test_path_grant_does_not_change_tier():
    """Path grants only touch allow_paths; they leave tier alone."""
    g = lingpai.Grant(
        id="pg1", granted_to="bob",
        issued_ts="2026-01-01T00:00:00+08:00",
        expires_ts="2099-01-01T00:00:00+08:00",
        grant_type="path", paths=["notes/**"],
    )
    d = lvli.evaluate(
        _msg(attaches=[]), peer_tier="tier_a", policy=lvli.PolicyConfig(),
        allow_paths=[], deny_paths=[],
        path_grants=[g],
    )
    # tier_a still → human_required even with a path grant present
    assert d.action == "human_required"


def test_load_effective_tier_grant_picks_highest(root_dir):
    _seed(root_dir, "p")
    lingpai.mint_tier_grant("p", "bob", "tier_b", ttl="1h")
    lingpai.mint_tier_grant("p", "bob", "tier_c", ttl="1h")
    g = lingpai.load_effective_tier_grant("p", "bob")
    assert g is not None
    assert g.target_tier == "tier_c"


def test_load_effective_tier_grant_isolates_peer(root_dir):
    _seed(root_dir, "p")
    lingpai.mint_tier_grant("p", "alice", "tier_c", ttl="1h")
    assert lingpai.load_effective_tier_grant("p", "alice").target_tier == "tier_c"
    assert lingpai.load_effective_tier_grant("p", "bob") is None


# ---------------------------------------------------------------------------
# PR-4.1 — consume_on_use + hit tracking
# ---------------------------------------------------------------------------


def test_record_hit_increments(root_dir):
    _seed(root_dir, "p")
    g = lingpai.mint_grant("p", "bob", ["x.md"], ttl="1h")
    assert lingpai.record_hit("p", g.id) is True
    assert lingpai.record_hit("p", g.id) is True
    g2 = lingpai.find_grant("p", g.id)
    assert g2.hit_count == 2
    assert g2.last_hit_ts is not None


def test_mark_consumed_makes_inactive(root_dir):
    _seed(root_dir, "p")
    g = lingpai.mint_tier_grant("p", "bob", "tier_c", ttl="1h", consume_on_use=True)
    assert g.is_active() is True
    assert lingpai.mark_consumed("p", g.id) is True
    g2 = lingpai.find_grant("p", g.id)
    assert g2.consumed_ts is not None
    assert g2.is_active() is False
    # And load_effective_tier_grant must skip it
    assert lingpai.load_effective_tier_grant("p", "bob") is None


def test_e2e_consume_on_use_fires_exactly_once(project_with_self_peer):
    """A tier grant with consume_on_use should let exactly one message
    through; the next falls back to the peer's configured tier."""
    identity = project_with_self_peer
    # Peer is tier_c in the fixture — change to tier_a so we see the upgrade.
    peers_yaml = bangjiao.project_peers_yaml_path("p")
    raw = yaml.safe_load(peers_yaml.read_text())
    raw["peers"][0]["policy_tier"] = "tier_a"
    peers_yaml.write_text(yaml.safe_dump(raw))

    g = lingpai.mint_tier_grant("p", "bob", "tier_c", ttl="1h", consume_on_use=True)

    # First message — should hit the grant and auto_pass.
    msg1 = _signed(identity, attaches=["bus/zongguan/inbox/x.md"])
    _, body1 = _round_trip("p", msg1)
    assert body1["decision"] == "auto_pass"

    # Grant should now be consumed.
    after = lingpai.find_grant("p", g.id)
    assert after.consumed_ts is not None

    # Second message — no live grant left, falls back to tier_a → human_required.
    msg2 = _signed(identity, attaches=["bus/zongguan/inbox/y.md"])
    _, body2 = _round_trip("p", msg2)
    assert body2["decision"] == "human_required"


# ---------------------------------------------------------------------------
# PR-4.1 — CLI argv reorder robustness
# ---------------------------------------------------------------------------


def test_cli_reorder_handles_implicit_add():
    import lingpai_cli as gc
    assert gc._reorder_argv(["p", "bob", "x.md"]) == ["add", "p", "bob", "x.md"]


def test_cli_reorder_handles_explicit_subcmd():
    import lingpai_cli as gc
    assert gc._reorder_argv(["p", "list"]) == ["list", "p"]
    assert gc._reorder_argv(["p", "info", "abc"]) == ["info", "p", "abc"]
    assert gc._reorder_argv(["p", "revoke", "abc"]) == ["revoke", "p", "abc"]


def test_cli_reorder_keeps_flags_in_place():
    import lingpai_cli as gc
    # Implicit add with trailing flag — flag should ride along.
    assert gc._reorder_argv(["p", "bob", "x.md", "--ttl", "1h"]) == [
        "add", "p", "bob", "x.md", "--ttl", "1h",
    ]


def test_cli_reorder_tier_flag_implicit_add():
    import lingpai_cli as gc
    # No paths, tier grant via flag
    assert gc._reorder_argv(["p", "bob", "--tier", "tier_c"]) == [
        "add", "p", "bob", "--tier", "tier_c",
    ]


def test_cli_info_smoke(root_dir, capsys):
    import lingpai_cli as gc
    _seed(root_dir, "p")
    g = lingpai.mint_grant("p", "bob", ["notes/**"], ttl="1h")
    rc = gc.main(["p", "info", g.id])
    out = capsys.readouterr().out
    assert rc == 0
    assert "state         : active" in out
    assert "grant_type    : path" in out
    assert g.id in out
    assert "remaining     :" in out


def test_cli_info_handles_missing(root_dir, capsys):
    import lingpai_cli as gc
    _seed(root_dir, "p")
    rc = gc.main(["p", "info", "deadbeef"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no such grant" in err


def test_cli_revoke_io_split(root_dir, capsys):
    import lingpai_cli as gc
    _seed(root_dir, "p")
    rc = gc.main(["p", "revoke", "deadbeef"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no such grant" in err

    rc = gc.main(["p", "revoke", "../../etc"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "invalid grant id" in err


def test_cli_tier_grant_via_flag(root_dir, capsys):
    import lingpai_cli as gc
    _seed(root_dir, "p")
    rc = gc.main(["p", "bob", "--tier", "tier_c", "--once"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "grant_type    : tier" in out
    assert "target_tier   : tier_c" in out
    assert "consume_on_use: True" in out
    rows = lingpai.list_grants("p")
    assert len(rows) == 1
    assert rows[0].grant_type == "tier"


def test_cli_rejects_once_without_tier(root_dir, capsys):
    import lingpai_cli as gc
    _seed(root_dir, "p")
    rc = gc.main(["p", "bob", "x.md", "--once"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--once" in err


# ---------------------------------------------------------------------------
# PR-4.1 — grant_paths None robustness (gemini Info)
# ---------------------------------------------------------------------------


def test_evaluate_handles_none_grant_paths():
    """Passing grant_paths=None must not raise."""
    msg = _msg(attaches=["bus/zongguan/inbox/x.md"])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=["bus/zongguan/inbox/**"],
        deny_paths=[],
        grant_paths=None,
    )
    assert d.action == "auto_pass"


def test_evaluate_handles_none_path_grants():
    msg = _msg(attaches=[])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=[], deny_paths=[],
        path_grants=None, tier_grant=None,
    )
    assert d.action == "auto_pass"


# ---------------------------------------------------------------------------
# PR-4.1 — issued_by truncation (gemini Info)
# ---------------------------------------------------------------------------


def test_issued_by_truncated_to_128(root_dir):
    _seed(root_dir, "p")
    g = lingpai.mint_grant("p", "bob", ["x.md"], ttl="30m", issued_by="x" * 500)
    assert len(g.issued_by) == 128
