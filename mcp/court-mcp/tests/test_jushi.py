"""PR-8 jushi -- unit tests for extract / redact / writer / record."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import jushi_extract as je    # noqa: E402
import jushi_redact as jr     # noqa: E402
import jushi_writer as jw     # noqa: E402
import record                 # noqa: E402


# ---------------------------------------------------------------------------
# Redact
# ---------------------------------------------------------------------------

def test_redact_keyword_drops_row():
    res = jr.apply_rules("my password is hunter2", jr.RedactionRules())
    assert res.kept is False
    assert "password" in res.reason
    assert res.placeholder is not None


def test_redact_regex_private_ip_drops_row():
    res = jr.apply_rules("connect to 192.168.1.50:8765", jr.RedactionRules())
    assert res.kept is False
    assert "pattern" in res.reason


def test_redact_regex_long_hex_drops_row():
    text = "sha is abcdef0123456789abcdef0123456789abcdef01"
    res = jr.apply_rules(text, jr.RedactionRules())
    assert res.kept is False


def test_redact_clean_text_passes():
    res = jr.apply_rules("hello world", jr.RedactionRules())
    assert res.kept is True
    assert res.reason is None


def test_redact_extra_keyword_via_extend():
    base = jr.RedactionRules()
    ext = base.extend(extra_keywords=["my-secret-token"])
    assert jr.apply_rules("this contains my-secret-token here", ext).kept is False
    # original rules untouched
    assert jr.apply_rules("this contains my-secret-token here", base).kept is True


def test_redact_drop_mode_yields_no_placeholder():
    rules = jr.RedactionRules(mode="drop")
    res = jr.apply_rules("password=foo", rules)
    assert res.kept is False
    assert res.placeholder is None


# ---------------------------------------------------------------------------
# Extract: cursor + parsing
# ---------------------------------------------------------------------------

def test_cursor_roundtrip(tmp_path: Path):
    je.write_cursor(tmp_path, "sess-A", 123)
    assert je.read_cursor(tmp_path, "sess-A") == 123


def test_cursor_corrupted_resets_to_zero(tmp_path: Path):
    je.cursor_path(tmp_path, "sess-A").parent.mkdir(parents=True, exist_ok=True)
    je.cursor_path(tmp_path, "sess-A").write_text("not json")
    assert je.read_cursor(tmp_path, "sess-A") == 0


def _jsonl_row(rtype: str, role: str, content, *,
               uuid: str = "u-1", ts: str = "2026-05-19T10:00:00Z",
               cwd: str = "/x") -> str:
    return json.dumps({
        "type": rtype,
        "message": {"role": role, "content": content},
        "uuid": uuid,
        "timestamp": ts,
        "cwd": cwd,
        "sessionId": "sess",
    }) + "\n"


def test_extract_skips_non_message_rows(tmp_path: Path):
    jsonl = tmp_path / "sess.jsonl"
    jsonl.write_text(
        json.dumps({"type": "permission-mode", "permissionMode": "yes"}) + "\n"
        + json.dumps({"type": "file-history-snapshot", "snapshot": {}}) + "\n"
        + _jsonl_row("user", "user", "hello")
    )
    mem = tmp_path / "mem"
    turns = list(je.iter_new_turns(jsonl, mem))
    assert [t.role for t in turns] == ["user"]
    assert turns[0].text == "hello"


def test_extract_flattens_assistant_blocks(tmp_path: Path):
    jsonl = tmp_path / "sess.jsonl"
    content = [
        {"type": "text", "text": "part one"},
        {"type": "tool_use", "name": "foo"},  # ignored
        {"type": "text", "text": "part two"},
    ]
    jsonl.write_text(_jsonl_row("assistant", "assistant", content))
    turns = list(je.iter_new_turns(jsonl, tmp_path / "mem"))
    assert len(turns) == 1
    assert "part one" in turns[0].text
    assert "part two" in turns[0].text
    assert "tool_use" not in turns[0].text


def test_extract_cursor_advances_on_reread(tmp_path: Path):
    jsonl = tmp_path / "sess.jsonl"
    jsonl.write_text(_jsonl_row("user", "user", "first"))
    mem = tmp_path / "mem"

    assert len(list(je.iter_new_turns(jsonl, mem))) == 1
    # Second read with no new content: nothing.
    assert len(list(je.iter_new_turns(jsonl, mem))) == 0

    # Append a row, cursor catches just that.
    with jsonl.open("a") as f:
        f.write(_jsonl_row("assistant", "assistant", "second"))
    extracted = list(je.iter_new_turns(jsonl, mem))
    assert [t.text for t in extracted] == ["second"]


def test_extract_skips_slash_command_meta(tmp_path: Path):
    jsonl = tmp_path / "sess.jsonl"
    jsonl.write_text(_jsonl_row("user", "user", "<command-name>foo</command-name>"))
    assert list(je.iter_new_turns(jsonl, tmp_path / "mem")) == []


def test_extract_cwd_prefix_filter(tmp_path: Path):
    jsonl = tmp_path / "sess.jsonl"
    jsonl.write_text(
        _jsonl_row("user", "user", "in scope", cwd="/wanted/project/foo")
        + _jsonl_row("user", "user", "out of scope", cwd="/elsewhere", uuid="u-2")
    )
    turns = list(je.iter_new_turns(
        jsonl, tmp_path / "mem", cwd_prefixes=["/wanted"]
    ))
    assert [t.text for t in turns] == ["in scope"]


# ---------------------------------------------------------------------------
# Writer: session file + housekeeping
# ---------------------------------------------------------------------------

def _make_turn(session_id="s1", role="user", text="hi",
               ts="2026-05-19T10:00:00+00:00", uuid="uuid-1234") -> je.Turn:
    return je.Turn(
        session_id=session_id, role=role, timestamp=ts,
        cwd="/x", git_branch="main", text=text, uuid=uuid,
    )


def test_writer_creates_file_with_frontmatter(tmp_path: Path):
    res = jr.RedactionResult(kept=True)
    path = jw.append_turn(tmp_path, _make_turn(), res, project="demo")
    body = path.read_text()
    assert "---" in body
    assert "session_id: s1" in body
    assert "project: demo" in body
    assert "## user" in body


def test_writer_appends_same_session_same_day(tmp_path: Path):
    res = jr.RedactionResult(kept=True)
    p1 = jw.append_turn(tmp_path, _make_turn(role="user", text="one"),
                        res, project="demo")
    p2 = jw.append_turn(tmp_path, _make_turn(role="assistant", text="two",
                                              uuid="uuid-5678"),
                        res, project="demo")
    assert p1 == p2
    body = p1.read_text()
    # Frontmatter exactly once
    assert body.count("session_id: s1") == 1
    assert "one" in body and "two" in body


def test_writer_redact_placeholder_persists(tmp_path: Path):
    res = jr.RedactionResult(kept=False, reason="keyword=password",
                             placeholder="[redacted: keyword=password]")
    path = jw.append_turn(tmp_path, _make_turn(text="should-not-appear"),
                          res, project="demo")
    body = path.read_text()
    assert "[redacted: keyword=password]" in body
    assert "should-not-appear" not in body


def test_writer_drop_mode_writes_nothing(tmp_path: Path):
    res = jr.RedactionResult(kept=False, reason="keyword=password", placeholder=None)
    path = jw.append_turn(tmp_path, _make_turn(), res, project="demo")
    assert str(path) == "."


def test_writer_separate_file_per_day(tmp_path: Path):
    res = jr.RedactionResult(kept=True)
    p1 = jw.append_turn(tmp_path,
                        _make_turn(ts="2026-05-18T10:00:00+00:00"),
                        res, project="demo")
    p2 = jw.append_turn(tmp_path,
                        _make_turn(ts="2026-05-19T10:00:00+00:00",
                                   uuid="uuid-9999"),
                        res, project="demo")
    assert p1 != p2
    assert "2026-05-18" in str(p1)
    assert "2026-05-19" in str(p2)


def test_housekeeping_drops_old_days(tmp_path: Path):
    old_day = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d")
    young_day = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    (tmp_path / "sessions" / old_day).mkdir(parents=True)
    (tmp_path / "sessions" / young_day).mkdir(parents=True)
    (tmp_path / "sessions" / old_day / "x.md").write_text("x")
    (tmp_path / "sessions" / young_day / "y.md").write_text("y")

    removed = jw.housekeeping(tmp_path, retain_days=30)
    assert removed == 1
    assert not (tmp_path / "sessions" / old_day).exists()
    assert (tmp_path / "sessions" / young_day).exists()


def test_housekeeping_ignores_non_date_dirs(tmp_path: Path):
    (tmp_path / "sessions" / "not-a-date").mkdir(parents=True)
    (tmp_path / "sessions" / "not-a-date" / "x.md").write_text("x")
    removed = jw.housekeeping(tmp_path, retain_days=30)
    assert removed == 0
    assert (tmp_path / "sessions" / "not-a-date").exists()


# ---------------------------------------------------------------------------
# Record skill
# ---------------------------------------------------------------------------

def test_record_slug_kebab_ascii():
    assert record.slugify("Pick BM25 over TF-IDF") == "pick-bm25-over-tf-idf"


def test_record_slug_unicode_keeps_chars():
    s = record.slugify("选 BM25 评分")
    # \w with re.UNICODE keeps Chinese characters
    assert "bm25" in s
    assert "选" in s


def test_record_slug_pure_non_ascii_falls_back_to_hash():
    s = record.slugify("！！！")
    assert s.startswith("untitled-")


def test_record_write_decision(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    proj = "demo"
    (tmp_path / "projects" / proj).mkdir(parents=True)
    path = record.write_record(proj, "decision", "Switch to ed25519",
                               "Better than RSA-4096 for HTTP signing",
                               tags=["security"])
    text = path.read_text()
    assert "kind: decision" in text
    assert "title: Switch to ed25519" in text
    assert "tags: [security]" in text
    assert "Better than RSA-4096" in text


def test_record_list_orders_by_mtime(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COURT_ROOT", str(tmp_path))
    proj = "demo"
    (tmp_path / "projects" / proj).mkdir(parents=True)
    p1 = record.write_record(proj, "decision", "first", "a")
    p2 = record.write_record(proj, "note", "second", "b")
    # Touch the older one's mtime backward
    old = datetime.now().timestamp() - 3600
    os.utime(p1, (old, old))
    listed = record.list_records(proj)
    assert listed[0] == p2     # newest first
    assert p1 in listed
