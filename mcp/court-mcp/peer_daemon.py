"""agent-court — court-peer HTTP receiver daemon (per project).

Listens on the address configured by ``--bind`` / ``COURT_PEER_BIND``
(default ``0.0.0.0:8765``).

Endpoints:
- ``GET  /healthz``  — liveness probe for ``list_peers.reachable``.
- ``POST /inbox``    — accept a signed inter-court message and drop it into
                       ``$COURT_ROOT/projects/<project>/bus/<from_court>/inbox/``.

Per-project model:
- The daemon is started with ``court-peer <project>``.
- It reads ``court.yaml``'s ``federation:`` block. If ``enabled: false``
  (the default), the daemon refuses to start — federation is off for
  that project.
- It loads ``peers.yaml`` from the *same* project directory; peers
  registered there are the only ones permitted to POST.
- It loads the project's own keypair (the public key is what other
  peers verify against; the daemon itself only uses the public key
  identity for fingerprint reporting at startup).

PR-1 scope: signature verification + role whitelist enforcement
(``expose_roles``). PR-2 adds policy engine and path-level enforcement.
"""

from __future__ import annotations

import argparse
import os
import sys

from aiohttp import web

import grants
import judge
import policy
from peer_lib import (
    ReplayCache,
    UnsafeNameError,
    append_peer_error,
    assert_safe_path_component,
    court_root,
    iso_now,
    load_federation,
    load_identity,
    load_peers,
    project_court_yaml_path,
    project_dir,
    ts_is_fresh,
    verify_signature,
    write_inbound_to_bus,
)


REQUIRED_FIELDS = ("from", "from_court", "to", "body", "ts", "id", "signature")

# Per-field type expectations after REQUIRED_FIELDS presence check passes.
# ``attaches`` is optional but if present must be a list of strings.
_STRING_FIELDS = ("from", "from_court", "to", "body", "ts", "id", "signature")
_OPTIONAL_STRING_FIELDS = ("in_reply_to",)

# Drift this far from the receiver's clock (in seconds) and we reject the
# message as too stale or too far in the future. 5 minutes leaves room for
# normal NTP skew while keeping the replay window short.
_TS_FRESHNESS_WINDOW_SECONDS = 300


def _log(project: str, line: str) -> None:
    print(f"[{iso_now()}] [{project}] {line}", file=sys.stderr, flush=True)


def make_app(project: str) -> web.Application:
    """Build the aiohttp app for a project. Caller is expected to have already
    validated that federation is enabled — but we re-check on each request so a
    flipped flag in court.yaml takes effect without restart."""
    app = web.Application()
    app["project"] = project
    # Per-app replay cache (in-memory, bounded). Combined with the ts
    # freshness window, a captured request can be replayed at most once
    # before the cache learns it (rejected) and at most TS_WINDOW after
    # daemon restart (also rejected because the original ts is stale).
    app["replay_cache"] = ReplayCache(ttl_seconds=_TS_FRESHNESS_WINDOW_SECONDS * 2)
    app.router.add_get("/healthz", _healthz)
    app.router.add_post("/inbox", _inbox)
    return app


async def _healthz(request: web.Request) -> web.Response:
    project = request.app["project"]
    fed = load_federation(project)
    return web.json_response({
        "status": "ok",
        "project": project,
        "court_id": fed.court_id,
        "federation_enabled": fed.enabled,
    })


async def _inbox(request: web.Request) -> web.Response:
    project = request.app["project"]

    # Re-check federation on every request so toggling the flag in court.yaml
    # takes effect without restart.
    fed = load_federation(project)
    if not fed.enabled:
        append_peer_error(project, f"federation-disabled: rejecting inbound (project={project})")
        _log(project, "reject 403: federation disabled for this project")
        return web.json_response({"error": "federation_disabled"}, status=403)

    try:
        msg = await request.json()
    except Exception as e:
        append_peer_error(project, f"bad-json: {e}")
        _log(project, f"reject 400: bad JSON ({e})")
        return web.json_response({"error": "bad_json"}, status=400)

    if not isinstance(msg, dict):
        append_peer_error(project, f"bad-json-shape: top-level must be object, got {type(msg).__name__}")
        _log(project, "reject 400: top-level JSON is not an object")
        return web.json_response({"error": "bad_json_shape"}, status=400)

    missing = [k for k in REQUIRED_FIELDS if k not in msg]
    if missing:
        append_peer_error(project, f"missing-fields: {missing} from={msg.get('from_court')}")
        _log(project, f"reject 400: missing fields {missing}")
        return web.json_response({"error": "missing_fields", "fields": missing}, status=400)

    # Strict per-field type check. A malicious peer could send
    # ``body: {"x": 1}`` and trip ``.lower()`` deeper in the pipeline; or
    # ``attaches: "abc"`` so ACL iterates characters. We catch all of that
    # up front so the rest of the handler can assume well-typed input.
    bad_type = []
    for k in _STRING_FIELDS:
        if not isinstance(msg.get(k), str):
            bad_type.append(k)
    for k in _OPTIONAL_STRING_FIELDS:
        if k in msg and not isinstance(msg.get(k), str):
            bad_type.append(k)
    if "attaches" in msg:
        atts = msg.get("attaches")
        if not isinstance(atts, list) or not all(isinstance(a, str) for a in atts):
            bad_type.append("attaches")
    if bad_type:
        append_peer_error(
            project, f"bad-field-types: {bad_type} from={msg.get('from_court')}"
        )
        _log(project, f"reject 400: bad field types {bad_type}")
        return web.json_response(
            {"error": "bad_field_types", "fields": bad_type}, status=400,
        )

    from_court = msg["from_court"]

    # ts freshness — reject anything outside ±5 min of receiver clock.
    # Comes before signature check so an unauthenticated peer can't spend
    # our CPU on stale-but-cryptographically-fine messages.
    if not ts_is_fresh(msg["ts"], window_seconds=_TS_FRESHNESS_WINDOW_SECONDS):
        append_peer_error(
            project,
            f"stale-ts: from_court={from_court} ts={msg.get('ts')!r} id={msg.get('id')}",
        )
        _log(project, f"reject 400: stale or unparseable ts {msg.get('ts')!r}")
        return web.json_response(
            {"error": "stale_or_invalid_ts", "ts": msg.get("ts")}, status=400,
        )

    # 403 — sender not registered as a peer of this project.
    peers_cfg = load_peers(project)
    peer = peers_cfg.by_court_id(from_court)
    if peer is None:
        append_peer_error(project, f"unknown-sender: from_court={from_court}")
        _log(project, f"reject 403: unknown sender '{from_court}'")
        return web.json_response(
            {"error": "unknown_sender", "from_court": from_court},
            status=403,
        )

    # 401 — no key to verify against, or signature doesn't match.
    pub_b64 = peer.pub_key_b64
    if not pub_b64:
        append_peer_error(
            project, f"no-pubkey: peer '{from_court}' missing pub_key_b64 in peers.yaml"
        )
        _log(project, f"reject 401: no pub_key_b64 for peer '{from_court}'")
        return web.json_response(
            {"error": "missing_peer_pub_key", "from_court": from_court}, status=401,
        )

    signature = msg["signature"]
    if not verify_signature(msg, signature, pub_b64):
        append_peer_error(
            project, f"bad-signature: from_court={from_court} id={msg.get('id')}"
        )
        _log(project, f"reject 401: bad signature from '{from_court}' id={msg.get('id')}")
        return web.json_response(
            {"error": "bad_signature", "from_court": from_court}, status=401,
        )

    # Replay protection — only check after signature verification so an
    # unauthenticated attacker can't pollute the cache with bogus ids and
    # later deny legitimate messages that happen to collide.
    if not request.app["replay_cache"].check_and_add(msg["id"]):
        append_peer_error(
            project, f"replay: from_court={from_court} id={msg.get('id')}"
        )
        _log(project, f"reject 409: replay detected id={msg.get('id')}")
        return web.json_response(
            {"error": "replay_detected", "id": msg.get("id")}, status=409,
        )

    # 403 — target role not in expose_roles whitelist. Always check, even
    # if expose_roles is an empty list (user said "lock this down").
    target = msg["to"]
    if target not in fed.expose_roles:
        append_peer_error(
            project,
            f"role-not-exposed: from_court={from_court} to={target} "
            f"allowed={fed.expose_roles}",
        )
        _log(
            project,
            f"reject 403: role '{target}' not in expose_roles {fed.expose_roles}",
        )
        return web.json_response(
            {
                "error": "role_not_exposed",
                "to": target,
                "expose_roles": fed.expose_roles,
            },
            status=403,
        )

    # Validate path-component fields *before* we run the policy engine —
    # if a peer picked a hostile court_id / id / from / to value the engine
    # would happily decide on it and we'd still blow up at write time.
    try:
        for fld in ("from_court", "from", "to", "id"):
            assert_safe_path_component(msg[fld], field_name=fld)
    except UnsafeNameError as e:
        append_peer_error(project, f"unsafe-name: {e}")
        _log(project, f"reject 400: {e}")
        return web.json_response({"error": "unsafe_name", "detail": str(e)}, status=400)

    # PR-2 policy layer — runs after signature + role whitelist pass.
    # peer_tier comes from peers.yaml entry, falls back to policy.default_tier.
    # PR-4: load both path grants (widen allow_paths) and tier grants
    # (override peer_tier) for the inbound peer. Pass them structured so
    # the policy engine can attribute matches back to specific grant ids;
    # after evaluate we update those grants' hit_count / consumed_ts.
    policy_cfg = policy.load_policy(project)
    path_grants = grants.load_path_grants_for_peer(project, from_court)
    tier_grant = grants.load_effective_tier_grant(project, from_court)
    decision = policy.evaluate(
        msg,
        peer_tier=peer.policy_tier,
        policy=policy_cfg,
        allow_paths=fed.allow_paths,
        deny_paths=fed.deny_paths,
        path_grants=path_grants,
        tier_grant=tier_grant,
    )

    # Record hits and consume one-shot tier grants. Best-effort: a
    # failing rewrite logs to peer-errors.log but never blocks delivery.
    if decision.grant_hits:
        consumable_ids = {
            tier_grant.id
            for tier_grant in (tier_grant,)
            if tier_grant is not None and tier_grant.consume_on_use
        }
        for gid in decision.grant_hits:
            grants.record_hit(project, gid)
            if gid in consumable_ids:
                grants.mark_consumed(project, gid)

    # PR-3 — refine the `judge` tier via an LLM call. Anything else
    # (auto_pass / human_required / denied) is final and goes to disk now.
    if decision.action == "judge":
        decision = await judge.evaluate_with_llm(msg, project, decision)

    # Audit log: never block delivery on a log-write failure.
    try:
        policy.log_decision(project, msg, decision)
    except OSError as e:
        append_peer_error(project, f"policy-log-write-failed: {e}")
        _log(project, f"warn: policy-log write failed: {e}")

    subdir = policy.subdir_for(decision.action)
    try:
        fpath = write_inbound_to_bus(
            project,
            msg,
            subdir=subdir,
            policy_decision=decision.action,
            policy_reasons=decision.reasons,
        )
    except OSError as e:
        # Disk full / permission denied. The peer's message is lost but
        # they get a clean 5xx so they can retry; the daemon stays up.
        append_peer_error(project, f"bus-write-failed: {e}")
        _log(project, f"reject 503: bus write failed: {e}")
        return web.json_response(
            {"error": "bus_write_failed", "detail": str(e)}, status=503,
        )

    _log(
        project,
        f"accepted: from {from_court} ({msg.get('from')}) -> {target} "
        f"id={msg['id']} decision={decision.action} tier={decision.tier} "
        f"file={fpath}",
    )

    # Map decision → outer status. We always return 200: signature + role
    # checks already passed, so the network exchange itself is fine. The
    # policy outcome is conveyed in the ``decision`` field so the sender's
    # MCP tool can surface it back to the upstream LLM.
    #
    # After PR-3 the policy ``judge`` action is always refined into either
    # ``auto_pass`` or ``human_required`` before we get here, so no entry
    # for ``judge`` appears in this map. assert just in case the pipeline
    # ever forgets to refine.
    assert decision.action != "judge", "judge action must be refined before reply"
    status_map = {
        "auto_pass": "accepted",
        "human_required": "pending_approval",
        "denied": "denied",
    }
    return web.json_response({
        "status": status_map.get(decision.action, "accepted"),
        "decision": decision.action,
        "tier": decision.tier,
        "reasons": decision.reasons,
        "file_path": str(fpath),
        "id": msg["id"],
    })


def main() -> int:
    parser = argparse.ArgumentParser(description="court-peer HTTP receiver daemon")
    parser.add_argument("project", help="project name under $COURT_ROOT/projects/")
    parser.add_argument(
        "--bind",
        default=os.environ.get("COURT_PEER_BIND", "0.0.0.0:8765"),
        help="address:port to bind (default 0.0.0.0:8765, env COURT_PEER_BIND)",
    )
    args = parser.parse_args()

    project = args.project
    if not project_dir(project).is_dir():
        print(
            f"[court-peer] project '{project}' not found at {project_dir(project)}",
            file=sys.stderr,
        )
        return 1

    if not project_court_yaml_path(project).is_file():
        print(
            f"[court-peer] missing court.yaml at {project_court_yaml_path(project)}",
            file=sys.stderr,
        )
        return 1

    fed = load_federation(project)
    if not fed.enabled:
        print(
            f"[court-peer] federation is disabled for project '{project}'.",
            file=sys.stderr,
        )
        print(
            f"[court-peer] enable it in {project_court_yaml_path(project)} under the "
            f"`federation:` block (see projects/example/court.yaml for the schema).",
            file=sys.stderr,
        )
        return 1

    try:
        identity = load_identity(project)
    except FileNotFoundError as e:
        print(f"[court-peer] {e}", file=sys.stderr)
        return 1

    host, _, port_s = args.bind.partition(":")
    if not port_s:
        print(f"[court-peer] --bind must be host:port, got '{args.bind}'", file=sys.stderr)
        return 2
    port = int(port_s)

    app = make_app(project)
    _log(project, f"court-peer listening on {host}:{port}")
    _log(project, f"court_root={court_root()}")
    _log(project, f"court_id={fed.court_id} fingerprint={identity.fingerprint}")
    _log(
        project,
        f"expose_roles={fed.expose_roles}"
        + (" (locked down — no inbound dispatch will succeed)" if not fed.expose_roles else ""),
    )
    web.run_app(app, host=host, port=port, print=None, access_log=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
