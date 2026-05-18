"""Tests for the onboard helper module (PR-7).

Covers:
- token encode/decode round-trip + tamper rejection
- court_id safety check (rejects "../" etc.)
- peers.yaml append + idempotency on duplicate court_id
- court.yaml template render: bangjiao block + hostname substitution
- old-token warning (>7d) flagged but not rejected

Run with:
    cd mcp/court-mcp && .venv/bin/pytest tests/test_onboard.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import onboard  # noqa: E402


VALID_FIELDS = {
    "court_id": "alice-laptop",
    "pub_b64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "url": "http://192.168.1.50:8765",
    "fingerprint": "deadbeef1234abcd",
}


# ---------------------------------------------------------------------------
# Token codec
# ---------------------------------------------------------------------------

def test_token_roundtrip():
    token = onboard.encode_token(**VALID_FIELDS)
    decoded = onboard.decode_token(token)
    assert decoded.court_id == VALID_FIELDS["court_id"]
    assert decoded.pub_b64 == VALID_FIELDS["pub_b64"]
    assert decoded.url == VALID_FIELDS["url"]
    assert decoded.fingerprint == VALID_FIELDS["fingerprint"]
    assert decoded.age_warning is False


def test_token_tampered_rejected():
    token = onboard.encode_token(**VALID_FIELDS)
    # Flip a character in the middle of the base64 payload.
    bad = token[:20] + ("A" if token[20] != "A" else "B") + token[21:]
    with pytest.raises(ValueError):
        onboard.decode_token(bad)


def test_token_invalid_court_id_rejected():
    fields = dict(VALID_FIELDS, court_id="../escape")
    token = onboard.encode_token(**fields)
    with pytest.raises(ValueError, match="court_id"):
        onboard.decode_token(token)


def test_token_old_warning_not_rejection():
    old = datetime.now(timezone.utc) - timedelta(days=8)
    token = onboard.encode_token(**VALID_FIELDS, created_at=old.isoformat())
    decoded = onboard.decode_token(token)
    assert decoded.age_warning is True
    assert decoded.court_id == VALID_FIELDS["court_id"]


def test_token_bad_url_scheme_rejected():
    fields = dict(VALID_FIELDS, url="ftp://example.com")
    token = onboard.encode_token(**fields)
    with pytest.raises(ValueError, match="url"):
        onboard.decode_token(token)


# ---------------------------------------------------------------------------
# peers.yaml editor
# ---------------------------------------------------------------------------

def test_add_peer_appends_new_entry(tmp_path: Path):
    peers = tmp_path / "peers.yaml"
    # Seed with the canonical 3-peer example shape.
    peers.write_text(yaml.safe_dump({
        "self": {"court_id": "me", "pub_key_fingerprint": "x"},
        "peers": [
            {"name": "bob", "court_id": "bob-laptop", "url": "http://bob",
             "pub_key_fingerprint": "b", "pub_key_b64": "B",
             "relation": "child", "policy_tier": "tier_b"},
        ],
    }))

    added = onboard.add_peer_to_yaml(
        peers,
        court_id="newpeer",
        pub_b64="P",
        url="http://new",
        fingerprint="f",
    )
    assert added is True

    loaded = yaml.safe_load(peers.read_text())
    ids = [p["court_id"] for p in loaded["peers"]]
    assert ids == ["bob-laptop", "newpeer"]


def test_add_peer_idempotent_on_duplicate_court_id(tmp_path: Path):
    peers = tmp_path / "peers.yaml"
    peers.write_text(yaml.safe_dump({"peers": []}))

    first = onboard.add_peer_to_yaml(
        peers, court_id="x", pub_b64="P", url="http://x", fingerprint="f",
    )
    second = onboard.add_peer_to_yaml(
        peers, court_id="x", pub_b64="P", url="http://x", fingerprint="f",
    )
    assert first is True
    assert second is False

    loaded = yaml.safe_load(peers.read_text())
    assert len(loaded["peers"]) == 1


# ---------------------------------------------------------------------------
# court.yaml renderer
# ---------------------------------------------------------------------------

@pytest.fixture
def template_path() -> Path:
    return HERE.parent.parent.parent / "projects" / "example" / "court.example.yaml"


def test_write_court_yaml_min_bangjiao(tmp_path: Path, template_path: Path):
    out = tmp_path / "court.yaml"
    onboard.write_court_yaml(
        template_path, out, project="demo", hostname="laptop",
    )
    data = yaml.safe_load(out.read_text())
    assert data["session"] == "demo"
    assert data["bangjiao"]["enabled"] is True
    assert data["bangjiao"]["court_id"] == "laptop-demo"
    assert "foreman" in data["bangjiao"]["expose_roles"]


def test_write_court_yaml_hostname_substitution(tmp_path: Path, template_path: Path):
    out = tmp_path / "court.yaml"
    onboard.write_court_yaml(
        template_path, out, project="work", hostname="bob-mbp",
    )
    text = out.read_text()
    assert "{{HOSTNAME}}" not in text
    assert "{{PROJECT}}" not in text
    assert "bob-mbp-work" in text
