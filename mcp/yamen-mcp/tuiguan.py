"""agent-yamen — LLM judge for PR-2's ``judge`` decision branch (PR-3).

When the policy engine returns ``action="tuiguan"`` the daemon calls
:func:`evaluate_with_llm` here. We spawn the configured LLM CLI as a
subprocess, feed it the message + policy reasons, and parse the JSON
verdict it returns. The judge can only output one of two verdicts —
``auto_pass`` or ``human_required`` — so this stage refines the
``judge`` slot into a concrete delivery action.

Fail-safe design
----------------

Anything that can go wrong with an LLM call (binary missing, timeout,
parse failure, low confidence, network error) collapses to a single
fallback: **upgrade the message to ``human_required``** and surface
the failure reason in the audit log. The receiving court is never
worse off than it would have been without PR-3 — at worst the human
sees a few extra files that PR-2 alone would have auto-delivered.

Configuration
-------------

Read from the project's ``yamen.yaml``:

- ``bangjiao_block.tuiguan.cli``       Override for which CLI binary to use.
                                 Falls back to top-level ``default_cli``
                                 (which itself defaults to ``claude``).
- ``bangjiao_block.tuiguan.model``     Optional ``--model`` argument passed
                                 to the CLI. Omitted if unset.
- ``bangjiao_block.tuiguan.prompt_file``  Override system-prompt path.
                                 Falls back to the bundled
                                 ``prompts/tuiguan.md``.
- ``bangjiao_block.tuiguan.timeout_seconds``  Default 30.
- ``bangjiao_block.tuiguan.confidence_threshold``  Default 0.6. A verdict
                                 of ``auto_pass`` with a lower
                                 ``confidence`` is upgraded to
                                 ``human_required``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Optional

from lvli import Decision


HERE = Path(__file__).resolve().parent
BUILTIN_JUDGE_PROMPT = HERE / "prompts" / "tuiguan.md"


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def build_user_message(msg: dict, policy_decision: Decision) -> str:
    """Render the message into a deterministic block the judge can read.

    Fields are listed in a stable order so the LLM's output is more
    reproducible across runs of the same content. ``attaches`` and
    ``in_reply_to`` only appear when present.
    """
    lines: list[str] = []
    lines.append("---")
    for key in ("from", "from_court", "to", "ts", "id"):
        val = msg.get(key)
        if val is not None:
            lines.append(f"{key}: {val}")
    in_reply_to = msg.get("in_reply_to")
    if in_reply_to:
        lines.append(f"in_reply_to: {in_reply_to}")
    attaches = msg.get("attaches") or []
    if attaches:
        lines.append(f"attaches: {json.dumps(attaches, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(msg.get("body", ""))
    lines.append("")
    lines.append("Policy reasons that led to judge:")
    for reason in policy_decision.reasons:
        lines.append(f"- {reason}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------

def _find_balanced_json_object(text: str) -> Optional[str]:
    """Return the first top-level ``{...}`` substring with balanced braces.

    Handles nested objects (``{"reason": "uses {markup} style"}``) and
    skips braces that appear inside JSON string literals. Returns the
    substring including the outer braces, or ``None`` if no balanced
    object is found.
    """
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    return text[start:i + 1]
    return None


def parse_verdict(raw: str) -> dict:
    """Extract ``{"verdict", "confidence", "reason"}`` from LLM output.

    The prompt asks for a single bare JSON object, but real-world LLMs
    drift: they wrap output in ```json fences, add an apology line,
    leave trailing whitespace, or write a ``reason`` field whose value
    contains its own ``{`` / ``}`` characters. We:
    1. strip markdown code fences,
    2. try a direct ``json.loads``,
    3. fall back to a balanced-brace scan that respects string literals.

    Returns the parsed dict. Raises ``ValueError`` if no balanced object
    can be found or required keys are absent — caller turns that into
    a ``human_required`` fallback Decision.
    """
    raw = (raw or "").strip()
    # Strip ```json ... ``` or ``` ... ``` fences if present.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        blob = _find_balanced_json_object(raw)
        if blob is None:
            raise ValueError(f"no JSON object found in LLM output: {raw[:200]!r}")
        try:
            data = json.loads(blob)
        except json.JSONDecodeError as e:
            raise ValueError(f"json object found but unparseable: {e}; blob={blob[:200]!r}")

    verdict = data.get("verdict")
    if verdict not in ("auto_pass", "human_required"):
        raise ValueError(f"verdict must be auto_pass or human_required, got {verdict!r}")
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        raise ValueError(f"confidence must be a number, got {data.get('confidence')!r}")
    return {
        "verdict": verdict,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(data.get("reason") or "").strip()[:300] or "(no reason given)",
    }


# ---------------------------------------------------------------------------
# Subprocess invocation
# ---------------------------------------------------------------------------

async def _invoke_cli(
    *,
    cli_path: str,
    model: Optional[str],
    system_prompt: str,
    user_message: str,
    timeout: float,
) -> str:
    """Run ``<cli_path> --append-system-prompt <text> [--model <model>] -p <user_message>``.

    ``--append-system-prompt`` takes the prompt **content** as a string,
    not a file path — so the caller is responsible for reading the prompt
    file off disk before invoking us. We pass the user message via stdin
    rather than ``-p`` to dodge ``ARG_MAX`` on long bodies and to avoid
    shell-quoting concerns (``create_subprocess_exec`` already bypasses
    the shell, but stdin is still the more robust channel for large
    inputs).

    Returns stdout. Raises asyncio.TimeoutError on timeout (caller turns
    it into a fallback). RuntimeError for non-zero exit / spawn fail.
    """
    args: list[str] = [
        cli_path,
        "--append-system-prompt",
        system_prompt,
    ]
    if model:
        args.extend(["--model", model])
    # ``-`` tells most CLIs (claude, codex) to read the prompt from stdin.
    # We still pass ``-p -`` so the CLI knows we're in "prompt" mode.
    args.extend(["-p", "-"])

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=user_message.encode()), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        # Drain to avoid ResourceWarning; the await is brief because the
        # process has just been signalled.
        try:
            await proc.communicate()
        except Exception:
            pass
        raise

    if proc.returncode != 0:
        raise RuntimeError(
            f"{cli_path} exited {proc.returncode}: {stderr_b.decode(errors='replace')[:300]}"
        )
    return stdout_b.decode(errors="replace")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def evaluate_with_llm(msg: dict, project: str, policy_decision: Decision) -> Decision:
    """Refine a ``judge`` decision into ``auto_pass`` or ``human_required``.

    Arguments:
        msg: the verified inbound peer message.
        project: project name (for loading yamen.yaml).
        policy_decision: the Decision returned by ``lvli.evaluate``.
            Its ``reasons`` are preserved and the LLM's verdict appended.

    Returns:
        A new ``Decision``:
        - ``action`` is one of ``auto_pass`` / ``human_required``;
        - ``tier`` is ``"llm_judge"`` on a clean LLM verdict, or
          ``"llm_judge_failed"`` on any fallback path.
    """
    from bangjiao import load_bangjiao

    fed = load_bangjiao(project)
    j = fed.tuiguan

    cli_name = j.cli or fed.default_cli
    cli_path = shutil.which(cli_name) if cli_name else None
    prompt_file = Path(j.prompt_file) if j.prompt_file else BUILTIN_JUDGE_PROMPT

    reasons = list(policy_decision.reasons)

    # Fallback 1: no CLI on PATH.
    if not cli_path:
        reasons.append(
            f"llm_judge_failed: cli '{cli_name}' not found on PATH → human_required"
        )
        return Decision(action="human_required", tier="llm_judge_failed", reasons=reasons)

    # Fallback 2: prompt file missing or unreadable on disk.
    try:
        system_prompt = prompt_file.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as e:
        reasons.append(
            f"llm_judge_failed: prompt_file {prompt_file} unreadable ({e}) → human_required"
        )
        return Decision(action="human_required", tier="llm_judge_failed", reasons=reasons)

    user_message = build_user_message(msg, policy_decision)

    # Fallback 3 / 4 / 5: timeout / non-zero exit / spawn failure.
    try:
        stdout = await _invoke_cli(
            cli_path=cli_path,
            model=j.model,
            system_prompt=system_prompt,
            user_message=user_message,
            timeout=float(j.timeout_seconds),
        )
    except asyncio.TimeoutError:
        reasons.append(
            f"llm_judge_failed: {cli_name} timed out after {j.timeout_seconds}s → human_required"
        )
        return Decision(action="human_required", tier="llm_judge_failed", reasons=reasons)
    except (RuntimeError, OSError) as e:
        reasons.append(f"llm_judge_failed: {e} → human_required")
        return Decision(action="human_required", tier="llm_judge_failed", reasons=reasons)

    # Fallback 6: LLM output unparseable.
    try:
        parsed = parse_verdict(stdout)
    except ValueError as e:
        reasons.append(f"llm_judge_failed: {e} → human_required")
        return Decision(action="human_required", tier="llm_judge_failed", reasons=reasons)

    verdict = parsed["verdict"]
    confidence = parsed["confidence"]
    judge_reason = parsed["reason"]

    reasons.append(
        f"llm_judge: verdict={verdict} confidence={confidence:.2f} reason={judge_reason!r}"
    )

    # Low-confidence auto_pass → upgrade to human_required. (We intentionally
    # do NOT downgrade a low-confidence human_required to auto_pass — the
    # safer fallback always wins.)
    if verdict == "auto_pass" and confidence < j.confidence_threshold:
        reasons.append(
            f"llm_judge: confidence {confidence:.2f} < threshold "
            f"{j.confidence_threshold:.2f} → upgraded to human_required"
        )
        return Decision(action="human_required", tier="llm_judge", reasons=reasons)

    return Decision(action=verdict, tier="llm_judge", reasons=reasons)
