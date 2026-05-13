"""agent-court — shenpi feishu (Lark) webhook channel.

POSTs a single chat-bot message to a Feishu custom bot's webhook URL.
The bot must be added to the chat the recipient is reading; that part is
out of scope here — we just hit the documented webhook contract.

Reference: https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot

Payload shape we use (the simplest one — no signing, no card):

  {
    "msg_type": "text",
    "content": {"text": "..."}
  }

Cards / signed webhooks are intentionally not implemented yet — start
simple, escalate if a user actually asks for them.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

# Network timeout for the webhook POST. Keep tight: a flaky webhook
# should fail fast and fall through to the next channel rather than
# blocking the daemon.
_HTTP_TIMEOUT = 5.0


async def send(item, shenpi_cfg) -> None:
    cfg = shenpi_cfg.feishu
    if not cfg.webhook_url:
        raise ValueError("feishu.webhook_url is not configured")

    text = _format_text(item, cfg.mention)
    payload = {"msg_type": "text", "content": {"text": text}}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _post() -> int:
        req = urllib.request.Request(
            cfg.webhook_url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return resp.status

    # ``urllib`` is synchronous; offload to a thread so we don't block
    # the event loop while waiting on a remote webhook.
    try:
        status = await asyncio.to_thread(_post)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        raise RuntimeError(f"feishu webhook failed: {e}") from e
    if status >= 400:
        raise RuntimeError(f"feishu webhook returned HTTP {status}")


def _format_text(item, mentions: list[str]) -> str:
    reasons = ", ".join(item.reasons[:3]) if item.reasons else "(no policy reasons)"
    body_excerpt = (item.body or "").strip().splitlines()
    excerpt = body_excerpt[0][:200] if body_excerpt else ""
    parts = [
        f"【agent-court 留中提醒】",
        f"court: {item.project}",
        f"from peer: {item.peer}  (msg_from={item.msg_from} → {item.msg_to})",
        f"id: {item.msg_id}",
        f"reasons: {reasons}",
    ]
    if excerpt:
        parts.append(f"body: {excerpt}")
    parts.append(f"approve: court-approve {item.project} approve {item.msg_id}")
    parts.append(f"deny:    court-approve {item.project} deny {item.msg_id}")
    if mentions:
        parts.append(" ".join(f"<at user_id=\"{m}\"></at>" for m in mentions))
    return "\n".join(parts)


from shenpi import _register_channel  # noqa: E402

_register_channel("feishu", send)
