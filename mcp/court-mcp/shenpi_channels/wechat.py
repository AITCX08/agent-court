"""agent-court — shenpi wechat channel (via cc-connect bridge).

Pushes an outbound notification into a specific WeChat conversation by
invoking the ``cc-connect send`` CLI as a subprocess.

cc-connect ([1]) is an independent bridge that maintains its own
per-conversation sessions; it knows how to deliver a message back to a
WeChat (or other IM platform) chat thread via the
``CC_PROJECT`` + ``CC_SESSION_KEY`` environment pair. We don't try to
introspect cc-connect's API — we just shell out, exactly the way an
in-cc-connect claude does when it wants to send an attachment.

[1] https://github.com/chenhg5/cc-connect

Inbound (the user typing "approve abc12345" in WeChat) doesn't need any
extra wiring here: cc-connect's claude session sees the agent-court MCP
server and can call ``pizhun(project, id, action)`` directly.
"""

from __future__ import annotations

import asyncio
import os
import shutil


async def send(item, shenpi_cfg) -> None:
    cfg = shenpi_cfg.wechat
    if not cfg.cc_connect_project:
        raise ValueError("wechat channel requires cc_connect_project")

    binary = shutil.which(cfg.cc_connect_bin)
    if binary is None:
        raise FileNotFoundError(
            f"cc-connect binary '{cfg.cc_connect_bin}' not found on PATH"
        )

    text = _format_text(item)
    env = {**os.environ, "CC_PROJECT": cfg.cc_connect_project}
    # session_key is optional — when unset, cc-connect "picks the first
    # active session" for the project, which is the common single-chat case.
    args = [binary, "send", "--project", cfg.cc_connect_project,
            "--message", text]
    if cfg.cc_connect_session_key:
        env["CC_SESSION_KEY"] = cfg.cc_connect_session_key
        args += ["--session", cfg.cc_connect_session_key]

    proc = await asyncio.create_subprocess_exec(
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.communicate()
        raise RuntimeError("cc-connect send timed out after 10s") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"cc-connect send exited {proc.returncode}: "
            f"{(stderr or b'').decode(errors='replace').strip()[:200]}"
        )


def _format_text(item) -> str:
    reasons = ", ".join(item.reasons[:3]) if item.reasons else "(no policy reasons)"
    excerpt = (item.body or "").strip().splitlines()
    excerpt_first = excerpt[0][:200] if excerpt else ""
    parts = [
        f"【留中提醒】",
        f"court: {item.project}",
        f"peer: {item.peer}  (msg_from={item.msg_from} → {item.msg_to})",
        f"id: {item.msg_id}",
        f"reasons: {reasons}",
    ]
    if excerpt_first:
        parts.append(f"body: {excerpt_first}")
    parts.append(
        f"回复 'approve {item.msg_id}' 或 'deny {item.msg_id}' 完成审批 "
        f"(我会通过 MCP 工具 pizhun() 执行)。"
    )
    return "\n".join(parts)


from shenpi import _register_channel  # noqa: E402

_register_channel("wechat", send)
