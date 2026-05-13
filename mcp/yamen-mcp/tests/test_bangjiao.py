"""Tests for the peer-network layer (PR-1).

Covers:
- per-project keypair generation + reload roundtrip
- canonical JSON determinism
- sign / verify happy path + tamper detection
- bangjiao.yaml loader (with `relation` field, backward-compatible with `role`)
- federation enable/disable gating
- HTTP POST /inbox round-trip: good signature, bad signature, unknown
  sender, missing fields, role-not-exposed, federation-disabled

Run with:
    cd mcp/court-mcp && .venv/bin/pytest -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import yaml

# Make the parent dir importable as flat modules (peer_lib, peer_daemon).
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import bangjiao  # noqa: E402
import yiguan_daemon  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_project(root: Path, project: str, *, bangjiao_enabled: bool = True,
                  expose_roles: list[str] | None = None,
                  yamen_id: str | None = None) -> Path:
    """Create a minimal project skeleton at <root>/projects/<project>/."""
    pdir = root / "projects" / project
    (pdir / "bus").mkdir(parents=True)
    (pdir / "prompts").mkdir(parents=True)

    fed_block: dict = {}
    if bangjiao_enabled:
        fed_block = {
            "enabled": True,
            "expose_roles": expose_roles if expose_roles is not None else ["zongguan"],
        }
        if yamen_id is not None:
            fed_block["yamen_id"] = yamen_id

    # Pin default_cli to a name that definitely doesn't resolve so the
    # PR-3 judge falls back deterministically; PR-1 tests don't care
    # which judge branch runs, only that the network layer worked.
    court_yaml = {
        "project": project,
        "session": f"court-{project}",
        "attach_window": "zongguan",
        "default_cli": "intentionally-missing-cli-for-test-x9z",
        "roles": [{"name": "zongguan", "prompt": "zongguan.md", "work_dir": "/tmp"}],
    }
    if fed_block:
        court_yaml["bangjiao"] = fed_block

    (pdir / "yamen.yaml").write_text(yaml.safe_dump(court_yaml))
    return pdir


@pytest.fixture
def root_dir(tmp_path, monkeypatch):
    """Set YAMEN_ROOT to a fresh tmp dir."""
    root = tmp_path / "alice"
    root.mkdir()
    monkeypatch.setenv("YAMEN_ROOT", str(root))
    # macOS hostname suffix would make yamen_id unstable across machines;
    # pin it for deterministic tests.
    monkeypatch.setenv("COURT_HOSTNAME", "testhost")
    return root


@pytest.fixture
def example_project(root_dir):
    _seed_project(root_dir, "example")
    return "example"


@pytest.fixture
def example_identity(root_dir, example_project):
    return bangjiao.generate_keypair(example_project, force=True)


# ---------------------------------------------------------------------------
# Identity round-trip
# ---------------------------------------------------------------------------


def test_keygen_creates_priv_and_pub(root_dir, example_project):
    identity = bangjiao.generate_keypair(example_project)
    assert bangjiao.project_priv_key_path(example_project).is_file()
    assert bangjiao.project_pub_key_path(example_project).is_file()
    assert oct(bangjiao.project_priv_key_path(example_project).stat().st_mode)[-3:] == "600"
    assert len(identity.fingerprint) == 32
    assert identity.project == example_project


def test_keygen_refuses_overwrite_without_force(root_dir, example_project):
    bangjiao.generate_keypair(example_project)
    with pytest.raises(FileExistsError):
        bangjiao.generate_keypair(example_project)
    new = bangjiao.generate_keypair(example_project, force=True)
    assert new.fingerprint


def test_load_identity_matches_generated(example_identity, example_project):
    loaded = bangjiao.load_identity(example_project)
    assert loaded.pub_b64 == example_identity.pub_b64
    assert loaded.fingerprint == example_identity.fingerprint


def test_projects_isolated(root_dir):
    """Project A's keypair is invisible to project B and vice versa."""
    _seed_project(root_dir, "a")
    _seed_project(root_dir, "b")
    id_a = bangjiao.generate_keypair("a")
    id_b = bangjiao.generate_keypair("b")
    assert id_a.pub_b64 != id_b.pub_b64
    assert id_a.fingerprint != id_b.fingerprint
    # default yamen_id derived from project name
    fed_a = bangjiao.load_bangjiao("a")
    fed_b = bangjiao.load_bangjiao("b")
    assert fed_a.yamen_id == "testhost-a"
    assert fed_b.yamen_id == "testhost-b"


# ---------------------------------------------------------------------------
# Canonical JSON + signatures
# ---------------------------------------------------------------------------


def test_canonical_payload_is_deterministic(root_dir):
    msg1 = {"from": "u", "from_court": "a", "to": "f", "body": "h",
            "ts": "2026-05-11T10:00:00+08:00", "id": "abc123",
            "signature": "should-not-be-included"}
    msg2 = {"ts": "2026-05-11T10:00:00+08:00", "id": "abc123",
            "body": "h", "to": "f", "from_court": "a", "from": "u",
            "extra": "ignored", "signature": "totally-different"}
    assert bangjiao.canonical_payload(msg1) == bangjiao.canonical_payload(msg2)


def test_sign_and_verify_happy_path(example_identity):
    msg = {"from": "upstream", "from_court": "alice", "to": "zongguan",
           "body": "hi", "ts": "2026-05-11T10:00:00+08:00", "id": "deadbeef"}
    sig = bangjiao.sign_message(msg, example_identity.priv)
    assert bangjiao.verify_signature(msg, sig, example_identity.pub_b64) is True


def test_verify_rejects_tampered_body(example_identity):
    msg = {"from": "upstream", "from_court": "alice", "to": "zongguan",
           "body": "hi", "ts": "2026-05-11T10:00:00+08:00", "id": "deadbeef"}
    sig = bangjiao.sign_message(msg, example_identity.priv)
    tampered = dict(msg, body="MALICIOUS")
    assert bangjiao.verify_signature(tampered, sig, example_identity.pub_b64) is False


def test_verify_rejects_wrong_pubkey(root_dir, example_identity, example_project):
    msg = {"from": "u", "from_court": "a", "to": "f", "body": "h",
           "ts": "2026-05-11T10:00:00+08:00", "id": "deadbeef"}
    sig = bangjiao.sign_message(msg, example_identity.priv)
    # generate an unrelated keypair via a second project
    _seed_project(root_dir, "other")
    other = bangjiao.generate_keypair("other")
    assert bangjiao.verify_signature(msg, sig, other.pub_b64) is False


# ---------------------------------------------------------------------------
# bangjiao.yaml loader
# ---------------------------------------------------------------------------


def test_load_peers_missing_file_returns_empty(example_identity, example_project):
    peers = bangjiao.load_peers(example_project)
    assert peers.peers == []
    assert peers.self_fingerprint == example_identity.fingerprint


def test_load_peers_parses_entries_with_relation(example_identity, example_project):
    bangjiao.project_peers_yaml_path(example_project).write_text(yaml.safe_dump({
        "self": {"yamen_id": "alice"},
        "peers": [{
            "name": "Bob",
            "yamen_id": "bob",
            "url": "http://192.168.1.50:8765/",
            "pub_key_fingerprint": "bbbb",
            "pub_key_b64": "BBBB==",
            "relation": "child",
        }],
    }))
    peers = bangjiao.load_peers(example_project)
    assert peers.self_yamen_id == "alice"
    bob = peers.by_yamen_id("bob")
    assert bob is not None
    assert bob.url == "http://192.168.1.50:8765"   # trailing slash stripped
    assert bob.relation == "child"


def test_load_peers_accepts_legacy_role_field(example_identity, example_project):
    """Older configs using `role:` (pre-PR-1 rename) should still load."""
    bangjiao.project_peers_yaml_path(example_project).write_text(yaml.safe_dump({
        "peers": [{
            "name": "Legacy",
            "yamen_id": "legacy",
            "url": "http://x",
            "pub_key_fingerprint": "x",
            "pub_key_b64": "X==",
            "role": "parent",
        }],
    }))
    peers = bangjiao.load_peers(example_project)
    legacy = peers.by_yamen_id("legacy")
    assert legacy is not None
    assert legacy.relation == "parent"


# ---------------------------------------------------------------------------
# Federation loader
# ---------------------------------------------------------------------------


def test_federation_disabled_by_default(root_dir):
    _seed_project(root_dir, "minimal", bangjiao_enabled=False)
    fed = bangjiao.load_bangjiao("minimal")
    assert fed.enabled is False
    assert fed.yamen_id == "testhost-minimal"


def test_bangjiao_enabled_reads_block(root_dir):
    _seed_project(
        root_dir, "open",
        bangjiao_enabled=True,
        yamen_id="custom-id",
        expose_roles=["zongguan", "auditor"],
    )
    fed = bangjiao.load_bangjiao("open")
    assert fed.enabled is True
    assert fed.yamen_id == "custom-id"
    assert fed.expose_roles == ["zongguan", "auditor"]


# ---------------------------------------------------------------------------
# Path glob helpers (schema-wired, PR-2 will use)
# ---------------------------------------------------------------------------


def test_path_allowed_deny_wins():
    assert bangjiao.path_allowed("prompts/zongguan.md", allow=["**"], deny=["prompts/**"]) is False


def test_path_allowed_no_allow_means_open():
    assert bangjiao.path_allowed("any/path.md", allow=[], deny=["other/**"]) is True


def test_path_allowed_must_match_allow():
    assert bangjiao.path_allowed(
        "bus/zongguan/inbox/x.md",
        allow=["bus/zongguan/inbox/**"],
        deny=[],
    ) is True
    assert bangjiao.path_allowed(
        "shared/leak.md",
        allow=["bus/zongguan/inbox/**"],
        deny=[],
    ) is False


# ---------------------------------------------------------------------------
# HTTP round-trip (POST /inbox)
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_self_peer(root_dir, example_project, example_identity):
    """Register a peer named 'bob' that *happens to use Alice's key* so we can
    sign + verify in one process. Also wire example_project's federation."""
    bangjiao.project_peers_yaml_path(example_project).write_text(yaml.safe_dump({
        "self": {"yamen_id": "alice"},
        "peers": [{
            "name": "Bob",
            "yamen_id": "bob",
            "url": "http://127.0.0.1:0",
            "pub_key_fingerprint": example_identity.fingerprint,
            "pub_key_b64": example_identity.pub_b64,
            "relation": "child",
        }],
    }))
    return example_identity


def _build_signed_msg(identity, *, from_court="bob", to="zongguan", body="hello"):
    import secrets
    msg = {
        "from": "upstream",
        "from_court": from_court,
        "to": to,
        "body": body,
        "ts": bangjiao.iso_now(),
        "id": secrets.token_hex(4),
    }
    msg["signature"] = bangjiao.sign_message(msg, identity.priv)
    return msg


async def _post_and_read(app, payload):
    import aiohttp
    from aiohttp.test_utils import TestServer

    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://127.0.0.1:{server.port}/inbox"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                return resp.status, await resp.json()
    finally:
        await server.close()


def _round_trip(app, payload):
    return asyncio.run(_post_and_read(app, payload))


def test_inbox_accepts_valid_signature(project_with_self_peer, example_project):
    """PR-1 scope: signature verification + on-disk delivery.

    Post-PR-3 the file's final subdir depends on the judge LLM (or its
    fallback); this test only cares that signature + role checks passed
    and a file was written somewhere. The specific routing logic is
    covered in tests/test_policy.py and tests/test_judge.py.
    """
    identity = project_with_self_peer
    app = yiguan_daemon.make_app(example_project)
    msg = _build_signed_msg(identity)
    status, body = _round_trip(app, msg)

    assert status == 200, body
    assert body["id"] == msg["id"]
    # File lives under bus/bob/<some-subdir>/<...>.md
    bus = bangjiao.project_bus_dir(example_project) / "bob"
    files = list(bus.glob("*/*.md"))
    assert len(files) == 1, files
    content = files[0].read_text()
    assert "from_court: bob" in content
    assert "to: zongguan" in content
    assert msg["body"] in content


def test_inbox_rejects_bad_signature(project_with_self_peer, example_project):
    identity = project_with_self_peer
    app = yiguan_daemon.make_app(example_project)
    msg = _build_signed_msg(identity)
    msg["body"] = "TAMPERED"

    status, body = _round_trip(app, msg)
    assert status == 401
    assert body["error"] == "bad_signature"


def test_inbox_rejects_unknown_sender(project_with_self_peer, example_project):
    identity = project_with_self_peer
    app = yiguan_daemon.make_app(example_project)
    msg = _build_signed_msg(identity, from_court="stranger")
    msg["signature"] = bangjiao.sign_message(msg, identity.priv)

    status, body = _round_trip(app, msg)
    assert status == 403
    assert body["error"] == "unknown_sender"


def test_inbox_rejects_missing_fields(project_with_self_peer, example_project):
    app = yiguan_daemon.make_app(example_project)
    status, body = _round_trip(app, {"from": "x"})
    assert status == 400
    assert body["error"] == "missing_fields"


def test_inbox_rejects_when_federation_disabled(root_dir):
    """A request to a project whose federation.enabled is false → 403."""
    _seed_project(root_dir, "private", bangjiao_enabled=False)
    app = yiguan_daemon.make_app("private")
    status, body = _round_trip(app, {
        "from": "u", "from_court": "x", "to": "zongguan",
        "body": "h", "ts": "now", "id": "1", "signature": "z",
    })
    assert status == 403
    assert body["error"] == "bangjiao_disabled"


def test_inbox_rejects_role_not_exposed(root_dir):
    """Even with valid signature, dispatching to a role not in expose_roles → 403."""
    _seed_project(root_dir, "scoped", expose_roles=["zongguan"])
    identity = bangjiao.generate_keypair("scoped")
    bangjiao.project_peers_yaml_path("scoped").write_text(yaml.safe_dump({
        "peers": [{
            "name": "Sibling",
            "yamen_id": "sibling-court",
            "url": "http://x",
            "pub_key_fingerprint": identity.fingerprint,
            "pub_key_b64": identity.pub_b64,
            "relation": "sibling",
        }],
    }))
    app = yiguan_daemon.make_app("scoped")
    # signed message targeting `backend` instead of foreman
    msg = _build_signed_msg(identity, from_court="sibling-court", to="backend")
    status, body = _round_trip(app, msg)
    assert status == 403
    assert body["error"] == "role_not_exposed"
    assert body["expose_roles"] == ["zongguan"]


# ---------------------------------------------------------------------------
# Hardening tests added in response to the multi-model audit:
# - replay protection
# - ts freshness
# - input type validation
# - path-component sanitization
# - malformed base64 in signature
# - expose_roles default behavior (fail-closed)
# ---------------------------------------------------------------------------


def test_inbox_rejects_replay_of_valid_message(project_with_self_peer, example_project):
    """A duplicate ``id`` from the same peer is rejected with 409 even
    though the signature still verifies. Belt-and-suspenders against an
    attacker who captures a legitimate request and replays it.

    Both POSTs share a single event loop and a single Application —
    aiohttp doesn't allow an Application to be reused across loops, so
    we can't just call ``_round_trip`` twice."""
    identity = project_with_self_peer
    app = yiguan_daemon.make_app(example_project)
    msg = _build_signed_msg(identity)

    async def _replay():
        import aiohttp
        from aiohttp.test_utils import TestServer
        server = TestServer(app)
        await server.start_server()
        try:
            url = f"http://127.0.0.1:{server.port}/inbox"
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=msg) as r1:
                    s1, b1 = r1.status, await r1.json()
                async with session.post(url, json=msg) as r2:
                    s2, b2 = r2.status, await r2.json()
            return (s1, b1), (s2, b2)
        finally:
            await server.close()

    (s1, _), (s2, b2) = asyncio.run(_replay())
    assert s1 == 200
    assert s2 == 409
    assert b2["error"] == "replay_detected"
    assert b2["id"] == msg["id"]


def test_inbox_rejects_stale_ts(project_with_self_peer, example_project):
    """A signed message with ts more than 5 min away from clock → 400."""
    identity = project_with_self_peer
    app = yiguan_daemon.make_app(example_project)
    msg = _build_signed_msg(identity)
    # Backdate ts by 1 hour. Need to re-sign since ts is in SIGNED_FIELDS.
    msg["ts"] = "2020-01-01T00:00:00+00:00"
    msg["signature"] = bangjiao.sign_message(msg, identity.priv)

    status, body = _round_trip(app, msg)
    assert status == 400
    assert body["error"] == "stale_or_invalid_ts"


def test_inbox_rejects_unparseable_ts(project_with_self_peer, example_project):
    identity = project_with_self_peer
    app = yiguan_daemon.make_app(example_project)
    msg = _build_signed_msg(identity)
    msg["ts"] = "yesterday-ish"
    msg["signature"] = bangjiao.sign_message(msg, identity.priv)

    status, body = _round_trip(app, msg)
    assert status == 400


def test_inbox_rejects_bad_field_types(project_with_self_peer, example_project):
    """Body as object / attaches as string get caught before policy runs."""
    identity = project_with_self_peer
    app = yiguan_daemon.make_app(example_project)

    msg = _build_signed_msg(identity)
    msg["body"] = {"not": "a string"}     # body must be string
    msg["signature"] = bangjiao.sign_message(msg, identity.priv)
    status, body = _round_trip(app, msg)
    assert status == 400
    assert body["error"] == "bad_field_types"
    assert "body" in body["fields"]


def test_inbox_rejects_attaches_as_string(project_with_self_peer, example_project):
    identity = project_with_self_peer
    app = yiguan_daemon.make_app(example_project)
    msg = _build_signed_msg(identity)
    msg["attaches"] = "not-a-list"
    msg["signature"] = bangjiao.sign_message(msg, identity.priv)
    status, body = _round_trip(app, msg)
    assert status == 400
    assert "attaches" in body["fields"]


def test_inbox_rejects_hostile_yamen_id(project_with_self_peer, example_project):
    """A registered peer can't pick a yamen_id like '../shared' to write
    outside bus/<peer>/. The daemon catches it as ``unsafe_name``.

    Setup: register a peer whose yamen_id contains a path-traversal
    sequence; a real peer would never get accepted into bangjiao.yaml in
    the first place, but we test that even if they did, the writer
    refuses."""
    identity = project_with_self_peer
    bad_peers = {
        "peers": [{
            "name": "Sneaky",
            "yamen_id": "../shared",
            "url": "http://x",
            "pub_key_fingerprint": identity.fingerprint,
            "pub_key_b64": identity.pub_b64,
            "relation": "sibling",
        }],
    }
    bangjiao.project_peers_yaml_path(example_project).write_text(yaml.safe_dump(bad_peers))

    msg = _build_signed_msg(identity, from_court="../shared")
    app = yiguan_daemon.make_app(example_project)
    status, body = _round_trip(app, msg)
    assert status == 400
    assert body["error"] == "unsafe_name"


def test_inbox_rejects_garbage_base64_signature(project_with_self_peer, example_project):
    """A non-base64 signature must be a clean 401, not a 500."""
    identity = project_with_self_peer
    app = yiguan_daemon.make_app(example_project)
    msg = _build_signed_msg(identity)
    msg["signature"] = "this is definitely not base64!!!"
    status, body = _round_trip(app, msg)
    assert status == 401
    assert body["error"] == "bad_signature"


def test_load_bangjiao_defaults_expose_roles_to_foreman(root_dir):
    """If yamen.yaml's federation block omits expose_roles, the loader
    must default to ['zongguan'] — never to '[]' which would expose all
    roles to inbound dispatch in the daemon."""
    _seed_project(root_dir, "implicit", expose_roles=None)
    # Drop the expose_roles key entirely so we test the implicit default.
    p = bangjiao.project_court_yaml_path("implicit")
    raw = yaml.safe_load(p.read_text())
    raw["bangjiao"].pop("expose_roles", None)
    p.write_text(yaml.safe_dump(raw))

    fed = bangjiao.load_bangjiao("implicit")
    assert fed.expose_roles == ["zongguan"]


# ---------------------------------------------------------------------------
# dispatch_to_peer MCP tool — error paths
# Direct call into server.dispatch_to_peer instead of going over HTTP, so
# the LLM-facing error surface is verified.
# ---------------------------------------------------------------------------


def test_dispatch_to_peer_unknown_project(root_dir):
    from server import guoshu_fanbang
    r = guoshu_fanbang(project="does-not-exist", peer_yamen_id="x", message="hi")
    assert r["error"] == "unknown_project"
    assert r["project"] == "does-not-exist"
    assert isinstance(r["available"], list)


def test_dispatch_to_peer_no_identity(root_dir):
    from server import guoshu_fanbang
    _seed_project(root_dir, "no-id")
    # Don't run keygen for "no-id" → load_identity will raise FileNotFoundError
    r = guoshu_fanbang(project="no-id", peer_yamen_id="x", message="hi")
    assert r["error"] == "no_identity"
    assert r["project"] == "no-id"


def test_dispatch_to_peer_unknown_peer(root_dir):
    from server import guoshu_fanbang
    _seed_project(root_dir, "lonely")
    bangjiao.generate_keypair("lonely")
    # No bangjiao.yaml written → empty peers list
    r = guoshu_fanbang(project="lonely", peer_yamen_id="ghost", message="hi")
    assert r["error"] == "unknown_peer"
    assert r["peer_yamen_id"] == "ghost"
    assert r["available"] == []


def test_dispatch_to_peer_transport_error(root_dir):
    """Pointing the peer at a closed port returns a clean transport_error
    dict — never raises."""
    from server import guoshu_fanbang
    _seed_project(root_dir, "isolated")
    identity = bangjiao.generate_keypair("isolated")
    bangjiao.project_peers_yaml_path("isolated").write_text(yaml.safe_dump({
        "peers": [{
            "name": "Bob",
            "yamen_id": "bob",
            "url": "http://127.0.0.1:1",   # port 1 is reserved → connection refused
            "pub_key_fingerprint": identity.fingerprint,
            "pub_key_b64": identity.pub_b64,
            "relation": "child",
        }],
    }))
    r = guoshu_fanbang(project="isolated", peer_yamen_id="bob", message="hi")
    assert r["error"] == "transport_error"
    assert r["project"] == "isolated"
    assert "id" in r   # the message id is included even on failure


def test_explicit_empty_expose_roles_locks_down(root_dir):
    """An explicit empty list means 'expose nothing' — every role is
    forbidden, including zongguan."""
    _seed_project(root_dir, "locked", expose_roles=[])
    identity = bangjiao.generate_keypair("locked")
    bangjiao.project_peers_yaml_path("locked").write_text(yaml.safe_dump({
        "peers": [{
            "name": "Sib", "yamen_id": "sib", "url": "http://x",
            "pub_key_fingerprint": identity.fingerprint,
            "pub_key_b64": identity.pub_b64, "relation": "sibling",
        }],
    }))
    app = yiguan_daemon.make_app("locked")
    msg = _build_signed_msg(identity, from_court="sib", to="zongguan")
    status, body = _round_trip(app, msg)
    assert status == 403
    assert body["error"] == "role_not_exposed"
