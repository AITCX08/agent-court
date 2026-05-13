"""End-to-end LAN walkthrough: Alice + Bob, two real projects, real HTTP.

The existing ``test_grants.py`` e2e cases use a single project posing as
both sides (the peer signs with the host project's own keypair). That
catches policy + daemon wiring but not the two-project setup an actual
deployment runs.

These tests stand up *two* projects in one process:

- ``alice`` — receiver (we drive a real aiohttp daemon for her)
- ``bob`` — sender (signs messages with his keypair; ``alice``'s
  ``peers.yaml`` carries his public key)

Then they exercise the documented LAN flow:

1. Bob sends a message with an attach outside Alice's static allow_paths
   → ``human_required``.
2. Alice mints a path grant for Bob → next message auto-passes.
3. Alice revokes → next message human_required again.
4. Alice mints a one-shot tier grant on a tier_a peer → exactly one
   message sails through, then it consumes itself.

These are the test plan items that were unchecked in PR #1.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import grants  # noqa: E402
import peer_daemon  # noqa: E402
import peer_lib  # noqa: E402


# ---------------------------------------------------------------------------
# Two-project fixture
# ---------------------------------------------------------------------------


def _seed_project(root: Path, name: str, *, allow_paths: list[str]) -> None:
    pdir = root / "projects" / name
    (pdir / "bus").mkdir(parents=True)
    (pdir / "prompts").mkdir(parents=True)
    fed = {
        "enabled": True,
        "expose_roles": ["foreman"],
        "allow_paths": allow_paths,
    }
    court_yaml = {
        "project": name,
        "session": f"court-{name}",
        "attach_window": "foreman",
        "default_cli": "intentionally-missing-cli-for-test-x9z",
        "roles": [{"name": "foreman", "prompt": "foreman.md", "work_dir": "/tmp"}],
        "federation": fed,
    }
    (pdir / "court.yaml").write_text(yaml.safe_dump(court_yaml))


@pytest.fixture
def two_courts(tmp_path, monkeypatch):
    """Set up alice + bob with cross-signed peers.yaml entries.

    Returns a dict ``{"alice_id", "bob_id"}`` of identities so the test
    can sign on Bob's behalf.
    """
    root = tmp_path / "court-root"
    root.mkdir()
    monkeypatch.setenv("COURT_ROOT", str(root))
    monkeypatch.setenv("COURT_HOSTNAME", "lan-test")

    _seed_project(root, "alice", allow_paths=["bus/foreman/inbox/**"])
    _seed_project(root, "bob", allow_paths=["bus/foreman/inbox/**"])

    alice_id = peer_lib.generate_keypair("alice", force=True)
    bob_id = peer_lib.generate_keypair("bob", force=True)

    # Alice trusts Bob (tier_a so we can see grants flip the decision).
    peer_lib.project_peers_yaml_path("alice").write_text(yaml.safe_dump({
        "peers": [{
            "name": "Bob's court",
            "court_id": "bob",
            "url": "http://127.0.0.1:0",
            "pub_key_fingerprint": bob_id.fingerprint,
            "pub_key_b64": bob_id.pub_b64,
            "relation": "sibling",
            "policy_tier": "tier_a",
        }],
    }))

    # Bob trusts Alice — symmetry is required by the daemon's identity
    # checks even though this test only drives traffic in one direction.
    peer_lib.project_peers_yaml_path("bob").write_text(yaml.safe_dump({
        "peers": [{
            "name": "Alice's court",
            "court_id": "alice",
            "url": "http://127.0.0.1:0",
            "pub_key_fingerprint": alice_id.fingerprint,
            "pub_key_b64": alice_id.pub_b64,
            "relation": "sibling",
            "policy_tier": "tier_b",
        }],
    }))

    return {"alice_id": alice_id, "bob_id": bob_id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signed_from_bob(bob_id, *, attaches=None, body="hi"):
    msg = {
        "from": "foreman",
        "from_court": "bob",
        "to": "foreman",
        "body": body,
        "ts": peer_lib.iso_now(),
        "id": secrets.token_hex(4),
    }
    if attaches:
        msg["attaches"] = list(attaches)
    msg["signature"] = peer_lib.sign_message(msg, bob_id.priv)
    return msg


async def _post_to(project, payload):
    """Spin up Alice's daemon in-process, POST one message, return (status, body)."""
    import aiohttp
    from aiohttp.test_utils import TestServer
    app = peer_daemon.make_app(project)
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
    return asyncio.run(_post_to(project, payload))


# ---------------------------------------------------------------------------
# Walkthrough
# ---------------------------------------------------------------------------


def test_path_grant_walkthrough(two_courts):
    """Documented LAN scenario: Bob → Alice over signed HTTP, with a
    path grant minted between two retries."""
    bob_id = two_courts["bob_id"]

    # Step 1: Bob's message references notes/, which is outside Alice's
    # static allow_paths. Bob is tier_a → without a grant, even matching
    # allow_paths would route via human_required (tier_a → human_required
    # is the soft-layer default), so we deliberately picture the simpler
    # "outside allow_paths" path: it's human_required for the hard
    # reason that the attach isn't covered.
    msg1 = _signed_from_bob(bob_id, attaches=["notes/q2.md"])
    status, body = _round_trip("alice", msg1)
    assert status == 200
    assert body["decision"] == "human_required"
    assert any("not covered by allow_paths" in r for r in body["reasons"])

    # Step 2: Alice mints a path grant.
    g = grants.mint_path_grant("alice", "bob", ["notes/**"], ttl="1h")
    assert g.grant_type == "path"

    # Step 3: Same shape of message — should auto_pass now, because the
    # grant widens allow_paths AND the soft tier_a check is irrelevant
    # (the hard "must match allow_paths" gate already passed).
    # Wait — tier_a soft layer still drives action. Let's check the
    # actual reason chain.
    msg2 = _signed_from_bob(bob_id, attaches=["notes/q2.md"])
    _, body2 = _round_trip("alice", msg2)
    # With path grant: hard layer passes (covered by grant), then soft
    # tier_a → human_required. So the *attach* check now passes; the
    # tier still requires human review. Verify that the reason chain
    # shows the grant fired.
    assert any("active grant pattern 'notes/**'" in r for r in body2["reasons"])

    # Step 4: Confirm hit_count was bumped from the grant firing.
    after = grants.find_grant("alice", g.id)
    assert after.hit_count >= 1


def test_path_grant_walkthrough_tier_c_peer(tmp_path, monkeypatch):
    """Same as above, but Bob is a tier_c peer so the grant actually
    flips the final decision auto_pass↔human_required, not just the
    attach check."""
    root = tmp_path / "court-root"
    root.mkdir()
    monkeypatch.setenv("COURT_ROOT", str(root))
    monkeypatch.setenv("COURT_HOSTNAME", "lan-test")

    _seed_project(root, "alice", allow_paths=["bus/foreman/inbox/**"])
    _seed_project(root, "bob", allow_paths=["bus/foreman/inbox/**"])
    alice_id = peer_lib.generate_keypair("alice", force=True)
    bob_id = peer_lib.generate_keypair("bob", force=True)

    peer_lib.project_peers_yaml_path("alice").write_text(yaml.safe_dump({
        "peers": [{
            "name": "Bob's court",
            "court_id": "bob",
            "url": "http://127.0.0.1:0",
            "pub_key_fingerprint": bob_id.fingerprint,
            "pub_key_b64": bob_id.pub_b64,
            "relation": "sibling",
            "policy_tier": "tier_c",  # auto_pass when allow_paths OK
        }],
    }))
    peer_lib.project_peers_yaml_path("bob").write_text(yaml.safe_dump({
        "peers": [{
            "name": "Alice's court",
            "court_id": "alice",
            "url": "http://127.0.0.1:0",
            "pub_key_fingerprint": alice_id.fingerprint,
            "pub_key_b64": alice_id.pub_b64,
            "relation": "sibling",
        }],
    }))

    # 1. Pre-grant: notes/ is outside allow_paths → human_required.
    msg1 = _signed_from_bob(bob_id, attaches=["notes/q2.md"])
    _, b1 = _round_trip("alice", msg1)
    assert b1["decision"] == "human_required"

    # 2. Mint grant
    g = grants.mint_path_grant("alice", "bob", ["notes/**"], ttl="1h")

    # 3. Same kind of attach → auto_pass (tier_c + grant covers the path)
    msg2 = _signed_from_bob(bob_id, attaches=["notes/q2.md"])
    _, b2 = _round_trip("alice", msg2)
    assert b2["decision"] == "auto_pass"
    assert any("active grant" in r for r in b2["reasons"])

    # 4. Revoke and confirm we're back to human_required.
    assert grants.revoke_grant("alice", g.id) == "revoked"
    msg3 = _signed_from_bob(bob_id, attaches=["notes/q2.md"])
    _, b3 = _round_trip("alice", msg3)
    assert b3["decision"] == "human_required"


def test_tier_grant_consume_on_use_walkthrough(two_courts):
    """Bob is tier_a (default human_required). A one-shot tier_c grant
    should let exactly one message auto_pass, then consume itself."""
    bob_id = two_courts["bob_id"]

    g = grants.mint_tier_grant(
        "alice", "bob", "tier_c", ttl="1h", consume_on_use=True,
    )

    msg1 = _signed_from_bob(bob_id, attaches=["bus/foreman/inbox/x.md"])
    _, b1 = _round_trip("alice", msg1)
    assert b1["decision"] == "auto_pass"
    assert any("tier grant active" in r for r in b1["reasons"])

    # Grant should now be consumed and not active.
    after = grants.find_grant("alice", g.id)
    assert after.consumed_ts is not None
    assert after.is_active() is False

    # Next message — no live grant, peer is tier_a → human_required.
    msg2 = _signed_from_bob(bob_id, attaches=["bus/foreman/inbox/y.md"])
    _, b2 = _round_trip("alice", msg2)
    assert b2["decision"] == "human_required"
