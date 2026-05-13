"""Tests for the policy engine (PR-2).

Covers:
- pure evaluate() decision matrix (tier_a/b/c, hard rules, peer override)
- HARDCODED deny paths and keywords (non-overridable)
- user-defined allow_paths / deny_paths from court.yaml
- policy.yaml extra_keywords appended to hardcoded list
- end-to-end HTTP round-trip with attaches → correct subdir on disk
- policy-log.jsonl audit trail

Run with:
    cd mcp/court-mcp && .venv/bin/pytest tests/test_policy.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import yiguan_daemon  # noqa: E402
import bangjiao  # noqa: E402
import lvli  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures (deliberately separate from test_peer.py — both files share the
# same root_dir style but we don't want a regression in one to silently
# break the other through fixture coupling)
# ---------------------------------------------------------------------------


def _seed(root: Path, project: str, *,
          expose_roles: list[str] | None = None,
          allow_paths: list[str] | None = None,
          deny_paths: list[str] | None = None,
          policy_yaml: dict | None = None) -> Path:
    pdir = root / "projects" / project
    (pdir / "bus").mkdir(parents=True)
    (pdir / "prompts").mkdir(parents=True)

    fed = {
        "enabled": True,
        "expose_roles": expose_roles if expose_roles is not None else ["foreman"],
    }
    if allow_paths is not None:
        fed["allow_paths"] = allow_paths
    if deny_paths is not None:
        fed["deny_paths"] = deny_paths

    # Pin default_cli to a name that definitely doesn't exist on PATH so
    # the PR-3 judge predictably falls back to ``llm_judge_failed``. Tests
    # that want to exercise a real (stubbed) judge live in test_judge.py.
    court_yaml = {
        "project": project,
        "session": f"court-{project}",
        "attach_window": "foreman",
        "default_cli": "intentionally-missing-cli-for-test-x9z",
        "roles": [{"name": "foreman", "prompt": "foreman.md", "work_dir": "/tmp"}],
        "federation": fed,
    }
    (pdir / "court.yaml").write_text(yaml.safe_dump(court_yaml))
    if policy_yaml is not None:
        (pdir / "policy.yaml").write_text(yaml.safe_dump(policy_yaml))
    return pdir


@pytest.fixture
def root_dir(tmp_path, monkeypatch):
    root = tmp_path / "alice"
    root.mkdir()
    monkeypatch.setenv("COURT_ROOT", str(root))
    monkeypatch.setenv("COURT_HOSTNAME", "testhost")
    return root


@pytest.fixture
def project_with_self_peer(root_dir):
    """Single project + a peer 'bob' whose pubkey is this project's own
    keypair, so the test can sign + the daemon can verify in one process."""
    _seed(root_dir, "p", expose_roles=["foreman", "auditor"])
    identity = bangjiao.generate_keypair("p", force=True)
    return _setup_peer(identity, "p")


def _setup_peer(identity, project, *, policy_tier=None):
    entry = {
        "name": "Bob",
        "court_id": "bob",
        "url": "http://127.0.0.1:0",
        "pub_key_fingerprint": identity.fingerprint,
        "pub_key_b64": identity.pub_b64,
        "relation": "child",
    }
    if policy_tier:
        entry["policy_tier"] = policy_tier
    bangjiao.project_peers_yaml_path(project).write_text(yaml.safe_dump({
        "peers": [entry],
    }))
    return identity


def _signed(identity, *, body="hello", to="foreman", attaches=None,
            from_court="bob"):
    import secrets
    msg = {
        "from": "upstream",
        "from_court": from_court,
        "to": to,
        "body": body,
        "ts": bangjiao.iso_now(),
        "id": secrets.token_hex(4),
    }
    if attaches:
        msg["attaches"] = list(attaches)
    msg["signature"] = bangjiao.sign_message(msg, identity.priv)
    return msg


async def _post(app, payload):
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
    return asyncio.run(_post(app, payload))


# ---------------------------------------------------------------------------
# Pure evaluate() — decision matrix
# ---------------------------------------------------------------------------


def _msg(**overrides):
    base = {"from_court": "bob", "to": "foreman", "body": "ok", "id": "x"}
    base.update(overrides)
    return base


def test_tier_a_pins_human_required():
    d = lvli.evaluate(_msg(), peer_tier="tier_a", policy=lvli.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "human_required"
    assert d.tier == "tier_a"


def test_tier_b_pins_judge():
    d = lvli.evaluate(_msg(), peer_tier="tier_b", policy=lvli.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "judge"


def test_tier_c_pins_auto_pass():
    d = lvli.evaluate(_msg(), peer_tier="tier_c", policy=lvli.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "auto_pass"


def test_default_tier_when_peer_omits():
    cfg = lvli.PolicyConfig(default_tier="tier_a")
    d = lvli.evaluate(_msg(), peer_tier=None, policy=cfg,
                        allow_paths=[], deny_paths=[])
    assert d.action == "human_required"


def test_unknown_tier_falls_back_to_human_required():
    d = lvli.evaluate(_msg(), peer_tier="tier_z", policy=lvli.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "human_required"
    assert any("unknown tier" in r for r in d.reasons)


# ---------------------------------------------------------------------------
# Hard rules — paths
# ---------------------------------------------------------------------------


def test_hardcoded_ssh_path_is_denied_even_for_tier_c():
    msg = _msg(attaches=["/home/alice/.ssh/id_ed25519"])
    d = lvli.evaluate(msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "denied"
    assert d.tier == "hard_rule"


def test_hardcoded_env_path_is_denied():
    msg = _msg(attaches=["app/.env"])
    d = lvli.evaluate(msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "denied"


def test_user_deny_path_is_denied():
    msg = _msg(attaches=["prompts/foreman.md"])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=[], deny_paths=["prompts/**"],
    )
    assert d.action == "denied"


def test_allow_paths_force_human_required_when_attach_outside():
    msg = _msg(attaches=["src/random.py"])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=["bus/foreman/inbox/**"], deny_paths=[],
    )
    assert d.action == "human_required"
    assert d.tier == "hard_rule"


def test_allow_paths_pass_when_every_attach_covered():
    msg = _msg(attaches=["bus/foreman/inbox/x.md", "bus/foreman/inbox/y.md"])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=["bus/foreman/inbox/**"], deny_paths=[],
    )
    assert d.action == "auto_pass"


def test_one_bad_attach_among_many_blocks_the_whole_message():
    msg = _msg(attaches=["bus/foreman/inbox/ok.md", "/etc/passwd"])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=[], deny_paths=[],
    )
    assert d.action == "denied"


# ---------------------------------------------------------------------------
# Hard rules — keywords
# ---------------------------------------------------------------------------


def test_hardcoded_keyword_forces_human_required():
    msg = _msg(body="here is my api_key=abcdef1234")
    d = lvli.evaluate(msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "human_required"
    assert d.tier == "hard_rule"


def test_keyword_match_is_case_insensitive():
    msg = _msg(body="here is my PASSWORD")
    d = lvli.evaluate(msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "human_required"


def test_policy_yaml_extra_keyword_is_honoured():
    cfg = lvli.PolicyConfig(extra_keywords=["merger", "wire transfer"])
    msg = _msg(body="re: the merger plan")
    d = lvli.evaluate(msg, peer_tier="tier_c", policy=cfg,
                        allow_paths=[], deny_paths=[])
    assert d.action == "human_required"


def test_clean_body_with_tier_c_is_auto_pass():
    msg = _msg(body="please review the new auth changes")
    d = lvli.evaluate(msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
                        allow_paths=[], deny_paths=[])
    assert d.action == "auto_pass"


# ---------------------------------------------------------------------------
# load_policy
# ---------------------------------------------------------------------------


def test_load_policy_missing_file_returns_defaults(root_dir):
    _seed(root_dir, "blank")
    cfg = lvli.load_policy("blank")
    assert cfg.default_tier == "tier_b"
    assert cfg.extra_keywords == []


def test_load_policy_reads_yaml(root_dir):
    _seed(root_dir, "tweaked", policy_yaml={
        "default_tier": "tier_a",
        "sensitive_keywords": ["acme", "alpha"],
    })
    cfg = lvli.load_policy("tweaked")
    assert cfg.default_tier == "tier_a"
    assert "acme" in cfg.extra_keywords


def test_load_policy_swallows_malformed_yaml(root_dir):
    _seed(root_dir, "broken")
    # write garbage
    (root_dir / "projects" / "broken" / "policy.yaml").write_text(": :: not yaml ::")
    cfg = lvli.load_policy("broken")
    assert cfg.default_tier == "tier_b"   # falls back to defaults


# ---------------------------------------------------------------------------
# peers.yaml policy_tier parsing
# ---------------------------------------------------------------------------


def test_peers_yaml_policy_tier_loaded(root_dir):
    _seed(root_dir, "scoped")
    identity = bangjiao.generate_keypair("scoped")
    _setup_peer(identity, "scoped", policy_tier="tier_a")
    peers = bangjiao.load_peers("scoped")
    bob = peers.by_court_id("bob")
    assert bob is not None
    assert bob.policy_tier == "tier_a"


def test_peers_yaml_policy_tier_optional(root_dir):
    _seed(root_dir, "scoped")
    identity = bangjiao.generate_keypair("scoped")
    _setup_peer(identity, "scoped", policy_tier=None)
    bob = bangjiao.load_peers("scoped").by_court_id("bob")
    assert bob.policy_tier is None


# ---------------------------------------------------------------------------
# HTTP end-to-end — daemon routes by decision
# ---------------------------------------------------------------------------


def test_e2e_clean_message_no_judge_cli_falls_back_pending(project_with_self_peer):
    """Without a judge CLI on PATH, PR-3's evaluate_with_llm falls back to
    ``human_required`` (the fail-safe behavior). Tests that exercise the
    actual auto_pass/human_required branches with a stubbed CLI live in
    tests/test_judge.py."""
    identity = project_with_self_peer
    app = yiguan_daemon.make_app("p")
    msg = _signed(identity, body="just a plain review request")
    status, body = _round_trip(app, msg)
    assert status == 200
    # default tier_b → judge → LLM unavailable → llm_judge_failed → human_required
    assert body["decision"] == "human_required"
    assert body["tier"] == "llm_judge_failed"
    pending = bangjiao.project_bus_dir("p") / "bob" / "pending-approval"
    assert len(list(pending.glob("*.md"))) == 1


def test_e2e_keyword_routes_to_pending_approval(project_with_self_peer):
    identity = project_with_self_peer
    app = yiguan_daemon.make_app("p")
    msg = _signed(identity, body="the prod password is hunter2")
    status, body = _round_trip(app, msg)
    assert status == 200
    assert body["decision"] == "human_required"
    assert body["status"] == "pending_approval"
    pending = bangjiao.project_bus_dir("p") / "bob" / "pending-approval"
    files = list(pending.glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "policy_decision: human_required" in content
    # nothing should have leaked into inbox
    inbox = bangjiao.project_bus_dir("p") / "bob" / "inbox"
    assert not list(inbox.glob("*.md"))


def test_e2e_attach_to_ssh_routes_to_denied(project_with_self_peer):
    identity = project_with_self_peer
    app = yiguan_daemon.make_app("p")
    msg = _signed(identity, body="have a look",
                  attaches=["~/.ssh/id_ed25519"])
    status, body = _round_trip(app, msg)
    assert status == 200
    assert body["decision"] == "denied"
    assert body["status"] == "denied"
    denied = bangjiao.project_bus_dir("p") / "bob" / "denied"
    files = list(denied.glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "policy_decision: denied" in content
    assert "id_ed25519" in content


def test_e2e_per_peer_tier_a_blocks_otherwise_clean_message(root_dir):
    _seed(root_dir, "strict")
    identity = bangjiao.generate_keypair("strict", force=True)
    _setup_peer(identity, "strict", policy_tier="tier_a")

    app = yiguan_daemon.make_app("strict")
    import secrets
    msg = {
        "from": "upstream",
        "from_court": "bob",
        "to": "foreman",
        "body": "fully clean message",
        "ts": bangjiao.iso_now(),
        "id": secrets.token_hex(4),
    }
    msg["signature"] = bangjiao.sign_message(msg, identity.priv)
    status, body = _round_trip(app, msg)
    assert status == 200
    assert body["decision"] == "human_required"
    assert body["tier"] == "tier_a"


def test_e2e_attaches_field_is_in_signed_payload(project_with_self_peer):
    """An attacker stripping/forging `attaches` after signing must fail verify."""
    identity = project_with_self_peer
    app = yiguan_daemon.make_app("p")
    msg = _signed(identity, body="hi", attaches=["bus/foreman/inbox/x.md"])
    # Forge: drop the attaches field but keep the signature.
    forged = dict(msg)
    forged.pop("attaches")
    status, body = _round_trip(app, forged)
    assert status == 401
    assert body["error"] == "bad_signature"


def test_policy_log_jsonl_captures_decision(project_with_self_peer):
    """Audit log captures the FINAL decision after PR-3 judge refinement —
    not the intermediate ``judge`` slot. With no LLM CLI present, the
    refinement falls back to ``human_required`` with tier
    ``llm_judge_failed``; the reasons array preserves the policy-layer
    chain plus the failure cause."""
    identity = project_with_self_peer
    app = yiguan_daemon.make_app("p")
    msg = _signed(identity, body="ok")
    _round_trip(app, msg)

    log_path = bangjiao.project_logs_dir("p") / "policy-log.jsonl"
    assert log_path.is_file()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["from_court"] == "bob"
    assert entry["to"] == "foreman"
    assert entry["action"] == "human_required"
    assert entry["tier"] == "llm_judge_failed"
    assert isinstance(entry["reasons"], list)
    # The policy layer's tier-decision reason is preserved through judge.
    assert any("tier=tier_b" in r for r in entry["reasons"])
    assert any("llm_judge_failed" in r for r in entry["reasons"])


# ---------------------------------------------------------------------------
# Hardening tests added in response to the multi-model audit:
# - path traversal in attaches → denied
# - case-insensitive hardcoded deny matching
# - non-string attach entries → denied
# - normalize_attach unit tests
# - expanded hardcoded deny patterns
# ---------------------------------------------------------------------------


def test_normalize_attach_rejects_traversal():
    assert lvli.normalize_attach("foo/../bar") is None
    assert lvli.normalize_attach("../etc/passwd") is None
    assert lvli.normalize_attach("a/b/../../../c") is None


def test_normalize_attach_strips_absolute_prefix():
    # absolute paths are rendered relative so the deny rule still bites
    assert lvli.normalize_attach("/etc/passwd") == "etc/passwd"
    assert lvli.normalize_attach("~/.ssh/id_rsa") == ".ssh/id_rsa"


def test_normalize_attach_handles_backslashes_and_drive():
    assert lvli.normalize_attach("C:\\Users\\alice\\.aws\\creds") == "Users/alice/.aws/creds"


def test_normalize_attach_rejects_non_strings():
    assert lvli.normalize_attach(None) is None
    assert lvli.normalize_attach(123) is None
    assert lvli.normalize_attach({"x": 1}) is None
    assert lvli.normalize_attach("") is None
    assert lvli.normalize_attach("   ") is None


def test_traversal_attach_short_circuits_to_denied():
    msg = _msg(attaches=["bus/foreman/inbox/../../identity/priv.key"])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=["bus/foreman/inbox/**"], deny_paths=[],
    )
    assert d.action == "denied"
    assert d.tier == "hard_rule"
    assert any("failed normalization" in r for r in d.reasons)


def test_case_insensitive_deny_path_match():
    """`.SSH/ID_RSA` must hit the `**/.ssh/**` rule even on macOS-style
    case-insensitive paths."""
    msg = _msg(attaches=[".SSH/ID_RSA"])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=[], deny_paths=[],
    )
    assert d.action == "denied"


def test_expanded_hardcoded_paths_match():
    """Cover several newly added hardcoded patterns at once."""
    examples = [
        ".npmrc",
        ".netrc",
        ".aws/credentials",
        "Library/Keychains/login.keychain-db",
        "etc/shadow",       # was /etc/shadow → "etc/shadow" after normalize
        "root/.bashrc",
        "var/lib/docker/aufs/diff/x",
        "secret.pem",
        "tls.key",
        "cert.p12",
    ]
    for path in examples:
        msg = _msg(attaches=[path])
        d = lvli.evaluate(
            msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
            allow_paths=[], deny_paths=[],
        )
        assert d.action == "denied", f"{path!r} should hit hardcoded deny"


def test_non_string_attach_entry_denied():
    msg = _msg(attaches=[42, "ok.md"])
    d = lvli.evaluate(
        msg, peer_tier="tier_c", policy=lvli.PolicyConfig(),
        allow_paths=[], deny_paths=[],
    )
    assert d.action == "denied"


def test_load_policy_clamps_threshold_and_timeout(root_dir):
    """Bad confidence_threshold / timeout in policy.yaml ↔ court.yaml fall
    back to safe defaults rather than carrying NaN / negative through the
    pipeline."""
    _seed(root_dir, "weird", policy_yaml={"default_tier": "tier_b"})
    # Inject bad values into court.yaml federation.judge block
    cyaml_path = bangjiao.project_court_yaml_path("weird")
    cyaml = yaml.safe_load(cyaml_path.read_text())
    cyaml["federation"]["judge"] = {
        "timeout_seconds": "not-a-number",
        "confidence_threshold": 99.0,
    }
    cyaml_path.write_text(yaml.safe_dump(cyaml))
    fed = bangjiao.load_bangjiao("weird")
    assert fed.tuiguan.timeout_seconds == 30.0
    assert fed.tuiguan.confidence_threshold == 1.0   # clamped from 99 → 1.0
