#!/usr/bin/env python3
"""
KeepingYouAwake multi-dialect LLM proxy (LiteLLM-backed).

Pipeline:
  client dialect (OpenAI / Anthropic / DeepSeek-compatible)
    → LiteLLM normalize
    → cursor-agent -p
         Anthropic /v1/messages → LLM mode (--mode ask, no tools): Claude/etc. own the tool loop
         OpenAI chat/completions → agent mode (--yolo): full coding agent behind the API
    → dialect response (incl. Anthropic SSE), streaming deltas live

Env:
  KYA_PROXY_HOST   default 127.0.0.1
  KYA_PROXY_PORT   default 8317
  KYA_PROXY_KEY    default sk-kya-cursor
  KYA_CURSOR_AGENT optional path to cursor-agent
  KYA_PROXY_EXEC_MODE  force "llm" or "agent" for all routes (optional)
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import litellm
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from litellm import CustomLLM, completion
from litellm.types.utils import GenericStreamingChunk
from litellm.utils import custom_llm_setup
import uvicorn

HOST = os.environ.get("KYA_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("KYA_PROXY_PORT", "8317"))
API_KEY = os.environ.get("KYA_PROXY_KEY", "sk-kya-cursor")
MODEL_ALIASES = [
    "cursor-auto",
    "cursor-auto[1m]",
    "auto",
    "auto[1m]",
    "BYOK-auto",
    "BYOK-auto[1m]",
    "byok-auto",
    "byok-auto[1m]",
    "deepseek-chat",
]

# Shared with HAPI cursorAutoRetry — covers Claude/Cursor/OpenAI clients via this proxy.
QUOTA_EXHAUSTED_RE = re.compile(
    r"(?:upgrade your plan to continue|"
    r"you(?:'ve| have) (?:hit|reached) (?:your |the )?(?:current |monthly |free )?"
    r"(?:quota|usage limit|limit)|"
    r"get cursor pro for more agent usage)",
    re.IGNORECASE,
)
TK_DIR = Path.home() / ".cursor-tk"
MAX_QUOTA_RETRIES = int(os.environ.get("KYA_PROXY_QUOTA_RETRIES", "2"))
QUOTA_WAIT_TIMEOUT_S = float(os.environ.get("KYA_PROXY_QUOTA_WAIT_S", "120"))
AGENT_TIMEOUT_S = float(os.environ.get("KYA_PROXY_AGENT_TIMEOUT_S", "180"))
# Per-request: "llm" (ask, no tools) | "agent" (full tools). Anthropic defaults to llm.
_EXEC_MODE: contextvars.ContextVar[str] = contextvars.ContextVar("kya_exec_mode", default="agent")
# Client dialect from User-Agent (+ path/headers fallback): claude|cursor|codex|deepseek|openai
_CLIENT_DIALECT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "kya_client_dialect", default="openai"
)
# Tools advertised by the client request (Anthropic or OpenAI shape).
_CLIENT_TOOLS: contextvars.ContextVar[List[dict]] = contextvars.ContextVar(
    "kya_client_tools", default=[]
)

ClientDialect = str  # claude | cursor | codex | deepseek | openai


def current_exec_mode() -> str:
    forced = (os.environ.get("KYA_PROXY_EXEC_MODE") or "").strip().lower()
    if forced in ("llm", "agent"):
        return forced
    mode = (_EXEC_MODE.get() or "agent").strip().lower()
    return mode if mode in ("llm", "agent") else "agent"


def current_client_dialect() -> str:
    d = (_CLIENT_DIALECT.get() or "openai").strip().lower()
    return d if d in ("claude", "cursor", "codex", "deepseek", "openai") else "openai"


def current_client_tools() -> List[dict]:
    return list(_CLIENT_TOOLS.get() or [])


def detect_client_dialect(
    *,
    user_agent: Optional[str] = None,
    path: str = "",
    anthropic_version: Optional[str] = None,
    extra_headers: Optional[dict] = None,
) -> str:
    """
    Identify the calling product from User-Agent (preferred) and request hints.
    Response wire format must match this dialect — never a custom KYA markup.
    """
    ua = (user_agent or "").lower()
    path_l = (path or "").lower()
    hdrs = {str(k).lower(): str(v) for k, v in (extra_headers or {}).items()}

    # Explicit product markers in UA
    if "claude-code" in ua or "claude code" in ua or "claude-cli" in ua:
        return "claude"
    if "anthropic" in ua:
        return "claude"
    if "codex" in ua or "openai-codex" in ua:
        return "codex"
    if "deepseek" in ua:
        return "deepseek"
    if "cursor" in ua:
        return "cursor"

    # Header hints
    if anthropic_version or hdrs.get("anthropic-version") or hdrs.get("x-api-key") and "/messages" in path_l:
        if "anthropic-version" in hdrs or anthropic_version:
            return "claude"
    if hdrs.get("openai-beta") and "responses" in path_l:
        return "codex"

    # Path fallback when UA is generic (python-httpx, undici, …)
    if path_l.endswith("/messages") or "/v1/messages" in path_l:
        return "claude"
    if "/responses" in path_l:
        return "codex"
    if "deepseek" in path_l:
        return "deepseek"
    return "openai"


def resolve_cursor_agent() -> str:
    env = os.environ.get("KYA_CURSOR_AGENT")
    if env and Path(env).is_file():
        return env
    home = Path.home()
    for p in (
        home / ".local/bin/cursor-agent",
        Path("/usr/local/bin/cursor-agent"),
        Path("/opt/homebrew/bin/cursor-agent"),
        home / ".local/bin/agent",
        Path("/usr/local/bin/agent"),
    ):
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    which = shutil.which("cursor-agent") or shutil.which("agent")
    if which:
        return which
    raise RuntimeError("cursor-agent not found")


def sync_auth_from_active_token() -> None:
    home = Path.home()
    token_path = home / ".cursor-tk" / "active_token"
    if not token_path.is_file():
        return
    token = token_path.read_text(encoding="utf-8", errors="ignore").strip()
    if len(token) < 20:
        return
    # cursor-agent 的 auth 位置随平台变：darwin 是 ~/.cursor，linux 读
    # XDG_CONFIG_HOME|~/.config/cursor。两处都写，容器和 macOS 都能认证。
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    for cursor_dir in (home / ".cursor", config_root / "cursor"):
        cursor_dir.mkdir(parents=True, exist_ok=True)
        auth_path = cursor_dir / "auth.json"
        auth: dict[str, Any] = {}
        if auth_path.is_file():
            try:
                auth = json.loads(auth_path.read_text(encoding="utf-8"))
            except Exception:
                auth = {}
        if auth.get("accessToken") == token:
            continue
        auth["accessToken"] = token
        auth.setdefault("refreshToken", token)
        auth_path.write_text(json.dumps(auth, indent=2), encoding="utf-8")


def flatten_content(content: Any) -> str:
    """Serialize message content including tool_use / tool_result for full context relay."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                t = block.get("type")
                if t == "tool_use":
                    name = block.get("name") or block.get("toolName") or "tool"
                    tid = block.get("id") or block.get("toolUseId") or ""
                    args = block.get("input") if "input" in block else block.get("arguments")
                    try:
                        args_s = json.dumps(args, ensure_ascii=False, default=str)
                    except Exception:
                        args_s = str(args)
                    parts.append(f"[tool_use id={tid} name={name}] {args_s}")
                    continue
                if t == "tool_result":
                    tid = block.get("tool_use_id") or block.get("toolUseId") or block.get("id") or ""
                    inner = flatten_content(block.get("content") or block.get("result"))
                    is_err = block.get("is_error") or block.get("isError")
                    tag = "tool_result_error" if is_err else "tool_result"
                    parts.append(f"[{tag} tool_use_id={tid}] {inner}")
                    continue
                if t in ("thinking", "redacted_thinking"):
                    think = block.get("thinking") or block.get("text") or ""
                    if think:
                        parts.append(f"[thinking] {think}")
                    continue
                if t in ("image", "input_image"):
                    parts.append("[image omitted]")
                    continue
                text = block.get("text") or block.get("content")
                if isinstance(text, str) and text:
                    parts.append(text)
                elif text is not None and not isinstance(text, str):
                    nested = flatten_content(text)
                    if nested:
                        parts.append(nested)
        return "\n".join(parts)
    if isinstance(content, dict):
        return flatten_content(content.get("text") or content.get("content") or content.get("thinking"))
    return str(content)


def messages_to_prompt(messages: list) -> str:
    """
    Forward the full client messages array as context — not only the last user turn.
    Claude/OpenAI clients already upload history; stripping it breaks multi-turn work.
    """
    if not messages:
        return ""
    if current_exec_mode() == "llm":
        tools = current_client_tools()
        dialect = current_client_dialect()
        lines = [
            f"You are a pure model backend for client dialect={dialect}.",
            "Do NOT search the filesystem or run shell yourself — the outer client executes tools.",
            "",
        ]
        if tools:
            names: List[str] = []
            for t in tools[:80]:
                if not isinstance(t, dict):
                    continue
                # Anthropic: name; OpenAI: function.name or name
                n = t.get("name")
                if not n and isinstance(t.get("function"), dict):
                    n = t["function"].get("name")
                if n:
                    names.append(str(n))
            name_hint = ", ".join(names[:40]) if names else "(see tool list in conversation)"
            if dialect == "claude":
                lines += [
                    "IMPORTANT: Reply in ONE shot. Do not explore with Cursor/Task/subagents.",
                    "Prefer emitting tool calls immediately after one short Chinese sentence.",
                    "FORBIDDEN: narrating next steps without tools (e.g. 接着读/接着核对/正在查找/把…钉死) and then stopping.",
                    "If you still need files or search, you MUST emit <invoke> in THIS same reply.",
                    "Only omit tools when giving a complete final answer to the user.",
                    "When you need tools, emit Anthropic-native tool calls (NOT markdown fences):",
                    "  <invoke name=\"TOOL_NAME\">",
                    "    <parameter name=\"arg\">value</parameter>",
                    "  </invoke>",
                    "You may include a short prose line before any <invoke>.",
                    f"Allowed tool names (subset): {name_hint}",
                    "If no tools are needed, output a complete prose answer only.",
                    "",
                ]
            else:
                # cursor / codex / deepseek / openai — OpenAI tool_calls JSON
                lines += [
                    "When you need tools, emit OpenAI-native tool_calls JSON only (one object):",
                    '{"tool_calls":[{"id":"call_1","type":"function","function":{"name":"TOOL_NAME","arguments":"{\\"arg\\":\\"value\\"}"}}]}',
                    "You may include a short prose line before the JSON object.",
                    f"Allowed function names (subset): {name_hint}",
                    "If no tools are needed, output prose only.",
                    "",
                ]
        else:
            lines += [
                "Output ONLY the next assistant message text.",
                "",
            ]
        lines += ["===== CONVERSATION (full) ====="]
    else:
        lines = [
            "You are continuing an existing multi-turn coding conversation.",
            "The client already provided full context below — use ALL of it (system, history, tools).",
            "Operate in full agent mode: use tools as needed; do not stop after a one-line plan.",
            "",
            "===== CONVERSATION (full) =====",
        ]
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "user").lower()
        name = m.get("name")
        header = f"[{role}" + (f" name={name}" if name else "") + "]"
        body = flatten_content(m.get("content"))
        # OpenAI-style tool calls on the message object
        tool_calls = m.get("tool_calls")
        if tool_calls:
            try:
                body = (body + "\n" if body else "") + "[tool_calls] " + json.dumps(
                    tool_calls, ensure_ascii=False, default=str
                )
            except Exception:
                body = (body + "\n" if body else "") + f"[tool_calls] {tool_calls}"
        if m.get("tool_call_id"):
            body = f"[tool_call_id={m.get('tool_call_id')}]\n" + (body or "")
        if not body and role not in ("system", "developer"):
            continue
        lines.append(header)
        lines.append(body or "(empty)")
        lines.append("")
    lines.append("===== END CONVERSATION =====")
    lines.append("Respond to the latest user request, using prior turns as context.")
    return "\n".join(lines).strip()


_FENCE_RE = re.compile(r"```(?:[\w-]+)?\n(.*?)```", re.DOTALL)
# Anthropic-native tool XML (Claude). Allow odd whitespace from bad joins.
_ANTHROPIC_INVOKE_RE = re.compile(
    r'<\s*invoke\s+name\s*=\s*["\']([^"\']+)["\']\s*>(.*?)<\s*/\s*invoke\s*>',
    re.IGNORECASE | re.DOTALL,
)
_ANTHROPIC_PARAM_RE = re.compile(
    r'<\s*parameter\s+name\s*=\s*["\']([^"\']+)["\']\s*>(.*?)<\s*/\s*parameter\s*>',
    re.IGNORECASE | re.DOTALL,
)
# OpenAI-style tool_calls JSON blob in free text
_OPENAI_TOOL_CALLS_RE = re.compile(
    r'\{\s*"tool_calls"\s*:\s*\[.*?\]\s*\}',
    re.DOTALL,
)


def _tool_name_set(tools: List[dict]) -> set:
    names = set()
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        n = t.get("name")
        if not n and isinstance(t.get("function"), dict):
            n = t["function"].get("name")
        if n:
            names.add(str(n))
    names.update({"Grep", "Glob", "Read", "Bash", "Edit", "Write", "Task", "TodoWrite"})
    return names


def _coerce_scalar(val: str) -> Any:
    v = val.strip()
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v


def _parse_kv_tool_block(body: str, allowed: set) -> Optional[dict]:
    """Fallback: fenced cards some models emit (name then key/value lines)."""
    raw_lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    if not raw_lines:
        return None
    name = raw_lines[0]
    if not name.startswith("mcp__") and name not in allowed:
        return None
    inputs: Dict[str, Any] = {}
    i = 1
    while i < len(raw_lines):
        key = raw_lines[i]
        if i + 1 >= len(raw_lines):
            break
        val = raw_lines[i + 1]
        if re.fullmatch(r"[A-Za-z0-9_.:-]+", key) and len(key) < 64:
            inputs[key] = _coerce_scalar(val)
            i += 2
        else:
            i += 1
    if not inputs and len(raw_lines) == 1:
        return None
    return {"name": name, "input": inputs}


def _dedupe_calls(calls: List[dict]) -> List[dict]:
    uniq: List[dict] = []
    seen = set()
    for c in calls:
        key = (c["name"], json.dumps(c.get("input") or {}, sort_keys=True, ensure_ascii=False))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


# "invoke Grep with pattern foo path /tmp glob *.{ts,tsx}"
_ENGLISH_INVOKE_RE = re.compile(
    r"(?i)\binvoke\s+([A-Za-z_][\w]*)\s+with\s+(.+?)(?=(?:\binvoke\s+[A-Za-z_]|\Z))",
    re.DOTALL,
)
_ENGLISH_KV_RE = re.compile(
    r"(?i)\b(pattern|path|glob|glob_pattern|target_directory|file_path|head_limit|title|"
    r"query|command|content|old_string|new_string|offset|limit|output_mode)\b\s+(.+?)(?="
    r"\b(?:pattern|path|glob|glob_pattern|target_directory|file_path|head_limit|title|"
    r"query|command|content|old_string|new_string|offset|limit|output_mode|invoke)\b|\Z)",
    re.DOTALL,
)
# Broken form: invoke Grep parameter name="pattern"VALUE parameter name="path"VALUE
_BROKEN_PARAM_INVOKE_RE = re.compile(
    r"(?i)\binvoke\s+([A-Za-z_][\w]*)\s+(parameter\s+name=.+?)(?=(?:\binvoke\s+[A-Za-z_]|\Z))",
    re.DOTALL,
)
_BROKEN_PARAM_KV_RE = re.compile(
    r'(?i)parameter\s+name\s*=\s*"([^"]+)"\s*(.*?)(?=(?:parameter\s+name\s*=|\Z))',
    re.DOTALL,
)


def _resolve_tool_name(name: str, allowed: set) -> Optional[str]:
    if name.startswith("mcp__") or name in allowed or not allowed:
        return name
    match = next((a for a in allowed if a.lower() == name.lower()), None)
    return match


def _parse_english_invoke_lines(text: str, allowed: set) -> Tuple[str, List[dict]]:
    """Parse prose-style invoke lines some models emit instead of XML."""
    calls: List[dict] = []
    remaining = text
    for m in _ENGLISH_INVOKE_RE.finditer(text):
        name = _resolve_tool_name(m.group(1), allowed)
        if not name:
            continue
        inputs: Dict[str, Any] = {}
        for km in _ENGLISH_KV_RE.finditer(m.group(2) or ""):
            inputs[km.group(1)] = _coerce_scalar(km.group(2).strip())
        if not inputs:
            continue
        calls.append({"name": name, "input": inputs})
        remaining = remaining.replace(m.group(0), "\n")
    for m in _BROKEN_PARAM_INVOKE_RE.finditer(remaining):
        name = _resolve_tool_name(m.group(1), allowed)
        if not name:
            continue
        inputs = {}
        for km in _BROKEN_PARAM_KV_RE.finditer(m.group(2) or ""):
            inputs[km.group(1)] = _coerce_scalar(km.group(2).strip())
        if not inputs:
            continue
        calls.append({"name": name, "input": inputs})
        remaining = remaining.replace(m.group(0), "\n")
    return remaining, calls


# Narration-only endings that mean the model intended more work but omitted tools.
_INCOMPLETE_TURN_RE = re.compile(
    r"(接着|继续|下一步|接下来|正在|准备|我来|我去|让我|先读|先查|核对|定位|钉死|"
    r"look(?:ing)? into|let me|i(?:'| a)?ll (?:check|read|search|look))"
    r"[^\n]{0,60}$",
    re.IGNORECASE,
)


def looks_like_incomplete_assistant_turn(prose: str, tool_calls: List[dict]) -> bool:
    """True when the model only announced next steps and emitted no tool_use."""
    if tool_calls:
        return False
    t = (prose or "").strip()
    if not t:
        return True
    if _INCOMPLETE_TURN_RE.search(t):
        return True
    # Very short narration without a conclusion — usually a dropped mid-turn.
    if len(t) < 100 and not any(
        m in t for m in ("结论", "原因", "建议", "可以", "无法", "已经", "不必", "正确用法", "总结")
    ):
        return True
    return False


def incomplete_turn_nudge(prompt: str) -> str:
    return (
        f"{prompt.rstrip()}\n\n"
        "===== SYSTEM NUDGE =====\n"
        "Your previous draft only narrated next steps and omitted tool calls.\n"
        "Emit the required <invoke>...</invoke> tool calls NOW "
        "(or give a complete final answer). Do not only say 接着/正在/核对.\n"
    )


def continue_after_quota_nudge(prompt: str, partial: str) -> str:
    """Resume the same assistant turn after Cursor quota renew mid-generation."""
    clipped = (partial or "").strip()
    if len(clipped) > 6000:
        clipped = clipped[-6000:]
    return (
        f"{prompt.rstrip()}\n\n"
        "===== CONTINUATION AFTER QUOTA RENEW =====\n"
        "Your previous draft was cut off by a usage-limit / account switch.\n"
        "Partial draft so far (do NOT repeat it; continue from the cut point):\n"
        f"---\n{clipped}\n---\n"
        "Continue the assistant reply. If tools are still needed, emit them now.\n"
    )


def extract_tool_calls_for_dialect(
    text: str,
    tools: List[dict],
    dialect: str,
) -> Tuple[str, List[dict]]:
    """
    Parse tool calls from model text using the client's native conventions.
    Returns (remaining_prose, [{name, input}, ...]).
    """
    if not text:
        return "", []
    text = _repair_exploded_text(text)
    allowed = _tool_name_set(tools)
    calls: List[dict] = []
    remaining = text
    dialect = (dialect or "openai").lower()

    if dialect == "claude":
        for m in _ANTHROPIC_INVOKE_RE.finditer(text):
            name = m.group(1).strip()
            if allowed and name not in allowed and not name.startswith("mcp__"):
                continue
            inputs: Dict[str, Any] = {}
            for pm in _ANTHROPIC_PARAM_RE.finditer(m.group(2) or ""):
                inputs[pm.group(1)] = _coerce_scalar(pm.group(2))
            calls.append({"name": name, "input": inputs})
            remaining = remaining.replace(m.group(0), "\n")
        # Fallback: models sometimes still dump tool cards as fences
        for m in _FENCE_RE.finditer(remaining):
            parsed = _parse_kv_tool_block(m.group(1), allowed)
            if not parsed:
                continue
            calls.append(parsed)
            remaining = remaining.replace(m.group(0), "\n")
        # Fallback: "invoke Grep with pattern X path Y glob Z"
        remaining, eng_calls = _parse_english_invoke_lines(remaining, allowed)
        calls.extend(eng_calls)
    else:
        # cursor / codex / deepseek / openai
        for m in _OPENAI_TOOL_CALLS_RE.finditer(text):
            try:
                obj = json.loads(m.group(0))
            except Exception:
                continue
            arr = obj.get("tool_calls") if isinstance(obj, dict) else None
            if not isinstance(arr, list):
                continue
            for item in arr:
                if not isinstance(item, dict):
                    continue
                fn = item.get("function") if isinstance(item.get("function"), dict) else {}
                name = fn.get("name") or item.get("name")
                if not name:
                    continue
                name = str(name)
                if allowed and name not in allowed and not name.startswith("mcp__"):
                    continue
                args_raw = fn.get("arguments") if fn else item.get("arguments")
                inp: Any = {}
                if isinstance(args_raw, dict):
                    inp = args_raw
                elif isinstance(args_raw, str) and args_raw.strip():
                    try:
                        inp = json.loads(args_raw)
                    except Exception:
                        inp = {"_raw": args_raw}
                calls.append({"name": name, "input": inp if isinstance(inp, dict) else {}})
            remaining = remaining.replace(m.group(0), "\n")
        for m in _FENCE_RE.finditer(remaining):
            body = m.group(1).strip()
            if '"tool_calls"' in body:
                try:
                    obj = json.loads(body)
                    arr = obj.get("tool_calls") if isinstance(obj, dict) else None
                    if isinstance(arr, list):
                        for item in arr:
                            if not isinstance(item, dict):
                                continue
                            fn = item.get("function") if isinstance(item.get("function"), dict) else {}
                            name = fn.get("name") or item.get("name")
                            if not name:
                                continue
                            args_raw = fn.get("arguments") if fn else {}
                            inp = {}
                            if isinstance(args_raw, str):
                                try:
                                    inp = json.loads(args_raw)
                                except Exception:
                                    inp = {}
                            elif isinstance(args_raw, dict):
                                inp = args_raw
                            calls.append({"name": str(name), "input": inp})
                        remaining = remaining.replace(m.group(0), "\n")
                        continue
                except Exception:
                    pass
            parsed = _parse_kv_tool_block(body, allowed)
            if parsed:
                calls.append(parsed)
                remaining = remaining.replace(m.group(0), "\n")

    prose = _dedupe_repeated_prose(re.sub(r"\n{3,}", "\n\n", remaining).strip())
    return prose, _dedupe_calls(calls)


# Back-compat alias used by older call sites / tests
def extract_anthropic_tool_calls(text: str, tools: List[dict]) -> Tuple[str, List[dict]]:
    return extract_tool_calls_for_dialect(text, tools, "claude")


def openai_chat_payload(
    *,
    model: str,
    prose: str,
    tool_calls: List[dict],
) -> dict:
    """OpenAI chat.completions shape (Cursor / Codex / DeepSeek / generic)."""
    message: Dict[str, Any] = {"role": "assistant", "content": prose or None}
    oai_calls = []
    for i, c in enumerate(tool_calls):
        oai_calls.append(
            {
                "id": f"call_{uuid.uuid4().hex[:20]}",
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": json.dumps(c.get("input") or {}, ensure_ascii=False),
                },
            }
        )
    if oai_calls:
        message["tool_calls"] = oai_calls
        if not prose:
            message["content"] = None
    finish = "tool_calls" if oai_calls else "stop"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def anthropic_message_payload(
    *,
    model: str,
    prose: str,
    tool_calls: List[dict],
) -> dict:
    content: List[dict] = []
    if prose:
        content.append({"type": "text", "text": prose})
    for c in tool_calls:
        content.append(
            {
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex[:24]}",
                "name": c["name"],
                "input": c.get("input") or {},
            }
        )
    if not content:
        content.append({"type": "text", "text": "(empty)"})
    stop = "tool_use" if tool_calls else "end_turn"
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def iter_anthropic_sse(payload: dict) -> Iterator[bytes]:
    """Emit Anthropic Messages SSE for a finished message (text ± tool_use)."""
    msg_id = payload["id"]
    model = payload.get("model") or "cursor-auto"
    content = payload.get("content") or []

    def pack(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")

    yield pack(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    for idx, block in enumerate(content):
        btype = block.get("type")
        if btype == "text":
            yield pack(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            text = block.get("text") or ""
            # modest chunks
            step = 80
            for i in range(0, len(text), step):
                yield pack(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {"type": "text_delta", "text": text[i : i + step]},
                    },
                )
            yield pack("content_block_stop", {"type": "content_block_stop", "index": idx})
        elif btype == "tool_use":
            yield pack(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": {},
                    },
                },
            )
            raw = json.dumps(block.get("input") or {}, ensure_ascii=False)
            yield pack(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "input_json_delta", "partial_json": raw},
                },
            )
            yield pack("content_block_stop", {"type": "content_block_stop", "index": idx})
    yield pack(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": payload.get("stop_reason") or "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 0},
        },
    )
    yield pack("message_stop", {"type": "message_stop"})


_STREAM_HOLD_MARKERS = (
    "<invoke",
    "\ninvoke ",
    " invoke ",
    "invoke grep",
    "invoke read",
    "invoke glob",
    "invoke edit",
    "invoke write",
    "invoke bash",
    "invoke task",
    "invoke shell",
    '{"tool_calls"',
)


def _find_stream_hold_cut(buf: str) -> Optional[int]:
    """Index where tool-markup may start, or None."""
    if not buf:
        return None
    lower = buf.lower()
    cut: Optional[int] = None
    for m in _STREAM_HOLD_MARKERS:
        i = lower.find(m)
        if i >= 0:
            cut = i if cut is None else min(cut, i)
    return cut


def _split_stream_hold(buf: str) -> Tuple[str, str]:
    """
    Split safe-to-stream prose from a holdback tail that may be starting tool markup.
    Keep a short rolling tail so markers split across chunks are not leaked as text.
    """
    if not buf:
        return "", ""
    cut = _find_stream_hold_cut(buf)
    if cut is not None:
        return buf[:cut], buf[cut:]
    keep = 48
    if len(buf) > keep:
        return buf[:-keep], buf[-keep:]
    return "", buf


def _flush_stream_hold(buf: str) -> Tuple[str, str]:
    """
    End-of-stream flush: emit all remaining prose; keep only confirmed tool markup.
    Without this, the rolling 48-char hold truncates every live response mid-sentence.
    """
    if not buf:
        return "", ""
    cut = _find_stream_hold_cut(buf)
    if cut is not None:
        return buf[:cut], buf[cut:]
    return buf, ""


def iter_anthropic_sse_live(
    prompt: str,
    *,
    model: str,
    tools: List[dict],
    dialect: str = "claude",
) -> Iterator[bytes]:
    """
    Live Anthropic Messages SSE: message_start ASAP on first prose delta,
    stream text_delta while holding back tool markup, then emit tool_use blocks.
    Quota renew+retry only before the first SSE byte.
    """

    def pack(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")

    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    last_err = "cursor-agent failed"
    text_open = False
    hold = ""
    raw_parts: List[str] = []

    # First SSE event immediately — Claude UI leaves "blank thinking" otherwise.
    yield pack(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model or "cursor-auto",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    def ensure_text_open() -> Iterator[bytes]:
        nonlocal text_open
        if text_open:
            return
        text_open = True
        yield pack(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )

    work_prompt = prompt
    for attempt_i in range(MAX_QUOTA_RETRIES + 1):
        outcome = _StreamAttempt()
        hold = ""
        def emit_text_delta(chunk: str) -> Iterator[bytes]:
            if not chunk:
                return
            yield from ensure_text_open()
            step = 80
            for i in range(0, len(chunk), step):
                yield pack(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": chunk[i : i + step]},
                    },
                )

        for delta in _iter_agent_stream_once(work_prompt, outcome):
            if not delta:
                continue
            hold += delta
            safe, hold = _split_stream_hold(hold)
            yield from emit_text_delta(safe)

        # Critical: flush rolling hold so the last ~48 chars are not dropped.
        safe, hold = _flush_stream_hold(hold)
        yield from emit_text_delta(safe)

        piece = (outcome.text or "").strip()
        if hold and hold not in piece:
            piece = f"{piece}\n{hold}".strip() if piece else hold
        if piece:
            raw_parts.append(piece)

        # Quota anytime: wait for 续杯, then continue the SAME SSE message.
        if outcome.quota:
            last_err = outcome.err or "quota exhausted"
            if attempt_i >= MAX_QUOTA_RETRIES:
                print(
                    "[kya_llm_proxy] quota retries exhausted; finalize with partial",
                    flush=True,
                )
            else:
                where = "mid-stream" if text_open or outcome.emitted else "before first token"
                print(
                    f"[kya_llm_proxy] quota {where}; renew+continue same SSE message",
                    flush=True,
                )
                _renew_and_prepare_retry(attempt_i, reason=f"quota {where}")
                partial = "\n".join(raw_parts).strip()
                work_prompt = (
                    continue_after_quota_nudge(prompt, partial)
                    if partial
                    else prompt
                )
                continue

        raw = "\n".join(raw_parts).strip()
        if not raw and not text_open:
            last_err = outcome.err or last_err
            # Close the already-opened message so client is not hung.
            yield pack(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 0},
                },
            )
            yield pack("message_stop", {"type": "message_stop"})
            raise RuntimeError(last_err)

        prose, tool_calls = extract_tool_calls_for_dialect(raw, tools, dialect)

        # One nudge if the model only said "接着读…" without tool_use — otherwise Claude ends the turn.
        if looks_like_incomplete_assistant_turn(prose, tool_calls) and tools:
            print(
                "[kya_llm_proxy] claude live: incomplete turn (narration, no tools); nudging once",
                flush=True,
            )
            nudge_outcome = _StreamAttempt()
            nudge_hold = ""
            for delta in _iter_agent_stream_once(incomplete_turn_nudge(prompt), nudge_outcome):
                if not delta:
                    continue
                nudge_hold += delta
                safe, nudge_hold = _split_stream_hold(nudge_hold)
                yield from emit_text_delta(safe)
            safe, nudge_hold = _flush_stream_hold(nudge_hold)
            yield from emit_text_delta(safe)
            nudge_raw = (nudge_outcome.text or "").strip()
            if nudge_hold and nudge_hold not in nudge_raw:
                nudge_raw = f"{nudge_raw}\n{nudge_hold}".strip() if nudge_raw else nudge_hold
            if nudge_raw:
                raw_parts.append(nudge_raw)
                raw = "\n".join(raw_parts).strip()
                prose, tool_calls = extract_tool_calls_for_dialect(raw, tools, dialect)

        if tool_calls:
            print(
                f"[kya_llm_proxy] claude live: mapped {len(tool_calls)} Anthropic tool_use",
                flush=True,
            )

        if text_open:
            yield pack("content_block_stop", {"type": "content_block_stop", "index": 0})
        elif prose:
            yield from ensure_text_open()
            step = 80
            for i in range(0, len(prose), step):
                yield pack(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": prose[i : i + step]},
                    },
                )
            yield pack("content_block_stop", {"type": "content_block_stop", "index": 0})
            text_open = True

        idx = 1 if text_open else 0
        for tc in tool_calls:
            tid = f"toolu_{uuid.uuid4().hex[:20]}"
            yield pack(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": tid,
                        "name": tc.get("name"),
                        "input": {},
                    },
                },
            )
            raw_in = json.dumps(tc.get("input") or {}, ensure_ascii=False)
            yield pack(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "input_json_delta", "partial_json": raw_in},
                },
            )
            yield pack("content_block_stop", {"type": "content_block_stop", "index": idx})
            idx += 1

        stop = "tool_use" if tool_calls else "end_turn"
        yield pack(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 0},
            },
        )
        yield pack("message_stop", {"type": "message_stop"})
        return

    yield pack(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 0},
        },
    )
    yield pack("message_stop", {"type": "message_stop"})
    raise RuntimeError(last_err)


def is_quota_exhausted(text: str) -> bool:
    return bool(text and QUOTA_EXHAUSTED_RE.search(text))


def _read_active_token() -> str:
    path = TK_DIR / "active_token"
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        pass
    return ""


def trigger_quota_switch(session_name: str = "kya-llm-proxy") -> None:
    """Wake KeepingYouAwake renewal (same handshake as HAPI cursor-acp)."""
    TK_DIR.mkdir(parents=True, exist_ok=True)
    quota_path = TK_DIR / "quota_exhausted"
    force_path = TK_DIR / "force_reconnect"
    context_path = TK_DIR / "hapi_switch_context.json"
    now_ms = int(time.time() * 1000)
    try:
        if quota_path.is_file() and (time.time() - quota_path.stat().st_mtime) < 3:
            print("[kya_llm_proxy] quota switch already requested <3s ago", flush=True)
            return
    except Exception:
        pass
    context_path.write_text(
        json.dumps(
            {
                "sessionName": session_name,
                "autoContinue": True,
                "timestamp": now_ms,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    quota_path.write_text(str(now_ms), encoding="utf-8")
    force_path.write_text("1", encoding="utf-8")
    print("[kya_llm_proxy] wrote ~/.cursor-tk/quota_exhausted (awaiting KYA renew)", flush=True)


def wait_for_quota_renewal(since_ms: float, timeout_s: float = QUOTA_WAIT_TIMEOUT_S) -> bool:
    """Poll KYA switch result / token change until renew succeeds or timeout."""
    result_path = TK_DIR / "hapi_switch_result.json"
    state_path = TK_DIR / "kya_switch_state.json"
    initial_token = _read_active_token()
    deadline = time.time() + timeout_s
    since_floor = since_ms - 1000
    last_log = 0.0
    while time.time() < deadline:
        time.sleep(0.5)
        now = time.time()
        if now - last_log >= 15:
            last_log = now
            print(
                f"[kya_llm_proxy] waiting for KYA renew… ({int(now - (deadline - timeout_s))}s)",
                flush=True,
            )
        try:
            if result_path.is_file():
                res = json.loads(result_path.read_text(encoding="utf-8"))
                ts = float(res.get("timestamp") or 0)
                if ts >= since_floor:
                    if res.get("success") is False:
                        print(f"[kya_llm_proxy] renew reported failure: {res.get('error')}", flush=True)
                        return False
                    if res.get("success") is True:
                        print(
                            f"[kya_llm_proxy] renew ok via hapi_switch_result ({res.get('email')})",
                            flush=True,
                        )
                        return True
        except Exception:
            pass
        try:
            if state_path.is_file():
                st = json.loads(state_path.read_text(encoding="utf-8"))
                ts = float(st.get("timestamp") or 0)
                if ts >= since_floor and st.get("status") == "success":
                    print(
                        f"[kya_llm_proxy] renew ok via kya_switch_state ({st.get('email')})",
                        flush=True,
                    )
                    return True
        except Exception:
            pass
        cur = _read_active_token()
        if cur and initial_token and cur != initial_token:
            print("[kya_llm_proxy] renew ok via active_token change", flush=True)
            return True
    print("[kya_llm_proxy] renew wait timed out", flush=True)
    return False


def _agent_popen(prompt: str, *, stream_json: bool) -> subprocess.Popen:
    if not prompt.strip():
        raise RuntimeError("empty prompt")
    sync_auth_from_active_token()
    agent = resolve_cursor_agent()
    mode = current_exec_mode()
    # llm: --mode ask, no --yolo → text-only completion (Claude BYOK must not nest a full agent).
    # agent: omit --mode + --yolo → full tools (OpenAI-style "agent behind API").
    args = [agent, "-p", "--model", "auto", "--trust"]
    if mode == "llm":
        args += ["--mode", "ask"]
        print("[kya_llm_proxy] exec_mode=llm (ask, no tools)", flush=True)
    else:
        args += ["--yolo"]
        print("[kya_llm_proxy] exec_mode=agent (yolo tools)", flush=True)
    workspace = os.environ.get("KYA_PROXY_WORKSPACE", "").strip()
    if workspace and Path(workspace).is_dir():
        args += ["--workspace", workspace]
    if stream_json:
        args += ["--output-format", "stream-json", "--stream-partial-output"]
    else:
        args += ["--output-format", "text"]
    args.append(prompt)
    env = os.environ.copy()
    env.pop("CURSOR_API_KEY", None)
    cwd = workspace if workspace and Path(workspace).is_dir() else str(Path.home())
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=cwd,
    )


def _diag_from_stream_obj(obj: dict) -> str:
    parts: List[str] = []
    typ = obj.get("type")
    blob = json.dumps(obj, ensure_ascii=False, default=str)
    if is_quota_exhausted(blob) or typ in ("result", "error") or obj.get("subtype") == "error":
        parts.append(blob)
    err = obj.get("error") or obj.get("message")
    if isinstance(err, str) and err:
        parts.append(err)
    elif isinstance(err, dict):
        parts.append(json.dumps(err, ensure_ascii=False, default=str))
    return "\n".join(parts)


# stream-json events that end an assistant "message" before the next one starts
_STREAM_MSG_BOUNDARY = frozenset(
    {"tool_call", "tool_result", "user", "result", "error"}
)


def _repair_exploded_text(text: str) -> str:
    """
    If partial deltas were wrongly joined with blank lines (token\\n\\ntoken),
    collapse them back. Real prose rarely has blank-line density this high.
    """
    if not text or "\n\n" not in text:
        return text
    if text.count("\n\n") > max(8, len(text) // 5):
        return text.replace("\n\n", "")
    return text


def _dedupe_repeated_prose(text: str) -> str:
    """Collapse consecutive duplicate / near-duplicate paragraphs (UI '同一句×3')."""
    if not text:
        return text
    parts = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not parts:
        return text.strip()
    out: List[str] = []
    for p in parts:
        if out:
            prev = out[-1]
            if p == prev:
                continue
            if prev.startswith(p):
                continue
            if p.startswith(prev) and len(p) > len(prev):
                out[-1] = p
                continue
        out.append(p)
    merged = "\n\n".join(out)

    def collapse_glued(s: str) -> str:
        changed = True
        while changed and s:
            changed = False
            max_n = min(240, len(s) // 2)
            for n in range(max_n, 11, -1):
                chunk = s[:n]
                if not chunk or not s.startswith(chunk + chunk):
                    continue
                times = 2
                while s.startswith(chunk * (times + 1)):
                    times += 1
                rest = s[n * times :]
                s = chunk + (("\n\n" + rest.lstrip()) if rest.strip() else "")
                changed = True
                break
        return s

    return collapse_glued(merged).strip()


def _absorb_assistant_snap(
    *,
    snap: str,
    last_snap: str,
    sealed: List[str],
    boundary: bool,
) -> Tuple[str, List[str], str, bool]:
    """
    Merge one assistant text snap.
    --stream-partial-output emits individual deltas (NOT cumulative snapshots).
    Long non-prefix snaps are a new assistant message (seal), not an append —
    appending full sentences caused the same line to repeat 3–4× in HAPI.
    Returns (last_snap, sealed, delta_to_yield, boundary_flag_False).
    """
    if not snap:
        return last_snap, sealed, "", False

    if snap == last_snap:
        return last_snap, sealed, "", False

    if boundary and last_snap:
        sealed = sealed + [last_snap]
        last_snap = snap
        return last_snap, sealed, snap, False

    if not last_snap:
        return snap, sealed, snap, False

    if snap.startswith(last_snap):
        delta = snap[len(last_snap) :]
        return snap, sealed, delta, False

    # New full-ish assistant message (not a tiny partial delta)
    if len(snap) >= 24 and len(last_snap) >= 24:
        sealed = sealed + [last_snap]
        return snap, sealed, snap, False

    # Short non-prefix → treat as stream-partial delta
    return last_snap + snap, sealed, snap, False


def _text_from_stream_json(stdout: str) -> Tuple[str, str]:
    """Return (assistant_text, diagnostic_blob) from cursor-agent stream-json."""
    sealed: List[str] = []
    last_snap = ""
    boundary = False
    diag_parts: List[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if is_quota_exhausted(line):
                diag_parts.append(line)
            continue
        typ = obj.get("type")
        if typ == "assistant":
            snap = flatten_content((obj.get("message") or {}).get("content"))
            last_snap, sealed, _delta, boundary = _absorb_assistant_snap(
                snap=snap, last_snap=last_snap, sealed=sealed, boundary=boundary
            )
            continue
        if typ in _STREAM_MSG_BOUNDARY:
            boundary = True
        d = _diag_from_stream_obj(obj)
        if d:
            diag_parts.append(d)
    parts = list(sealed)
    if last_snap:
        parts.append(last_snap)
    text = _repair_exploded_text("\n\n".join(parts).strip())
    return _dedupe_repeated_prose(text), "\n".join(diag_parts)


class _StreamAttempt:
    __slots__ = ("emitted", "quota", "err", "text")

    def __init__(self) -> None:
        self.emitted = False
        self.quota = False
        self.err = ""
        self.text = ""


def _renew_and_prepare_retry(attempt: int, *, reason: str = "quota") -> None:
    print(
        f"[kya_llm_proxy] {reason} "
        f"(attempt {attempt + 1}/{MAX_QUOTA_RETRIES + 1}); waiting for renew",
        flush=True,
    )
    since_ms = time.time() * 1000
    trigger_quota_switch()
    if not wait_for_quota_renewal(since_ms):
        raise RuntimeError("Cursor quota renew timed out or failed")
    sync_auth_from_active_token()
    time.sleep(1.0)


def _iter_agent_stream_once(prompt: str, attempt: _StreamAttempt) -> Iterator[str]:
    """
    Live-yield assistant text deltas from one cursor-agent run.
    Sets attempt.emitted / .quota / .err / .text; does not renew.
    """
    proc = _agent_popen(prompt, stream_json=True)
    assert proc.stdout is not None
    last_snap = ""
    sealed_parts: List[str] = []
    boundary = False
    diag_parts: List[str] = []
    deadline = time.time() + AGENT_TIMEOUT_S

    def _refresh_attempt_text() -> None:
        chunks = list(sealed_parts)
        if last_snap:
            chunks.append(last_snap)
        # Join real message breaks with blank line; never explode partial tokens.
        attempt.text = _dedupe_repeated_prose(
            _repair_exploded_text("\n\n".join(chunks).strip())
        )

    def emit_snapshot(snapshot: str) -> Iterator[str]:
        nonlocal last_snap, sealed_parts, boundary
        if not snapshot:
            return
        last_snap, sealed_parts, delta, boundary = _absorb_assistant_snap(
            snap=snapshot,
            last_snap=last_snap,
            sealed=sealed_parts,
            boundary=boundary,
        )
        _refresh_attempt_text()
        if delta:
            attempt.emitted = True
            yield delta

    def handle_line(line: str) -> Iterator[str]:
        nonlocal boundary
        line = line.strip()
        if not line:
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if is_quota_exhausted(line):
                diag_parts.append(line)
            return
        typ = obj.get("type")
        if typ == "assistant":
            snap = flatten_content((obj.get("message") or {}).get("content"))
            yield from emit_snapshot(snap)
            return
        if typ in _STREAM_MSG_BOUNDARY:
            boundary = True
        d = _diag_from_stream_obj(obj)
        if d:
            diag_parts.append(d)

    try:
        while True:
            if time.time() > deadline:
                proc.kill()
                attempt.err = "cursor-agent timeout"
                break
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.02)
                continue
            yield from handle_line(line)
        rest = proc.stdout.read() or ""
        for line in rest.splitlines():
            yield from handle_line(line)
    finally:
        err_s = ""
        try:
            if proc.stderr:
                err_s = (proc.stderr.read() or "").strip()
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        _refresh_attempt_text()
        diag = "\n".join(diag_parts)
        combined = "\n".join(p for p in (attempt.text, diag, err_s) if p)
        if is_quota_exhausted(combined):
            attempt.quota = True
            attempt.err = err_s or diag or "quota exhausted"
        elif not attempt.emitted:
            attempt.err = attempt.err or err_s or diag or "cursor-agent empty"


def run_cursor_agent_once(prompt: str) -> Tuple[str, bool, str]:
    """One agent invocation (buffered). Returns (text, quota_hit, error_detail)."""
    attempt = _StreamAttempt()
    # Consume live iterator without yielding to caller
    for _ in _iter_agent_stream_once(prompt, attempt):
        pass
    if attempt.quota:
        return attempt.text, True, attempt.err or "quota exhausted"
    if not attempt.text:
        return "", False, attempt.err or "cursor-agent empty"
    return attempt.text, False, ""


def run_cursor_agent_with_quota_retry(prompt: str) -> str:
    """
    Non-stream path: buffer full text.
    On quota (before or after partial text): wait for 续杯, then continue from the cut.
    """
    last_err = "cursor-agent failed"
    base_prompt = prompt
    work_prompt = prompt
    acc = ""
    for attempt_i in range(MAX_QUOTA_RETRIES + 1):
        text, quota, err = run_cursor_agent_once(work_prompt)
        if text:
            acc = f"{acc}\n{text}".strip() if acc else text
        if quota:
            last_err = err or "quota exhausted"
            if attempt_i >= MAX_QUOTA_RETRIES:
                if acc:
                    return acc
                break
            where = "mid-response" if acc else "before first token"
            _renew_and_prepare_retry(attempt_i, reason=f"quota {where}")
            work_prompt = (
                continue_after_quota_nudge(base_prompt, acc) if acc else base_prompt
            )
            continue
        if acc:
            return acc
        last_err = err or last_err
        raise RuntimeError(last_err)
    if acc:
        return acc
    raise RuntimeError(last_err)


def run_cursor_agent_text(prompt: str) -> str:
    """Non-stream: whole text blob (client did not ask for stream)."""
    return run_cursor_agent_with_quota_retry(prompt)


def run_cursor_agent_stream(prompt: str) -> Iterator[str]:
    """
    Live stream assistant deltas.
    On quota anytime: wait for 续杯, then continue yielding into the same client stream.
    """
    last_err = "cursor-agent failed"
    base_prompt = prompt
    work_prompt = prompt
    acc = ""
    for attempt_i in range(MAX_QUOTA_RETRIES + 1):
        outcome = _StreamAttempt()
        yield from _iter_agent_stream_once(work_prompt, outcome)
        if outcome.text:
            acc = f"{acc}\n{outcome.text}".strip() if acc else outcome.text
        if outcome.quota:
            last_err = outcome.err or "quota exhausted"
            if attempt_i >= MAX_QUOTA_RETRIES:
                return
            where = "mid-stream" if outcome.emitted or acc else "before first token"
            _renew_and_prepare_retry(attempt_i, reason=f"quota {where}")
            work_prompt = (
                continue_after_quota_nudge(base_prompt, acc) if acc else base_prompt
            )
            continue
        if outcome.emitted or acc:
            return
        last_err = outcome.err or last_err
        raise RuntimeError(last_err)
    raise RuntimeError(last_err)


class CursorAgentLLM(CustomLLM):
    def completion(
        self,
        model,
        messages,
        api_base,
        custom_prompt_dict,
        model_response,
        print_verbose,
        encoding,
        api_key,
        logging_obj,
        optional_params,
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers={},
        timeout=None,
        client=None,
    ):
        prompt = messages_to_prompt(messages)
        model_response.choices[0].message.content = run_cursor_agent_text(prompt)
        return model_response

    def streaming(
        self,
        model,
        messages,
        api_base,
        custom_prompt_dict,
        model_response,
        print_verbose,
        encoding,
        api_key,
        logging_obj,
        optional_params,
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers={},
        timeout=None,
        client=None,
    ) -> Iterator[GenericStreamingChunk]:
        prompt = messages_to_prompt(messages)
        for delta in run_cursor_agent_stream(prompt):
            yield GenericStreamingChunk(
                text=delta,
                is_finished=False,
                finish_reason="",
                usage=None,
                index=0,
                tool_use=None,
            )
        yield GenericStreamingChunk(
            text="",
            is_finished=True,
            finish_reason="stop",
            usage=None,
            index=0,
            tool_use=None,
        )


litellm.custom_provider_map = [
    {"provider": "kya_cursor", "custom_handler": CursorAgentLLM()}
]
custom_llm_setup()
# silence noisy litellm logs by default
litellm.suppress_debug_info = True

app = FastAPI(title="KYA LiteLLM Proxy", version="2.0")
UPSTREAM_MODEL = "kya_cursor/auto"


def check_auth(
    authorization: Optional[str] = None,
    x_api_key: Optional[str] = None,
) -> None:
    if not API_KEY:
        return
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_api_key:
        token = x_api_key.strip()
    if token != API_KEY:
        raise HTTPException(status_code=401, detail={"error": {"message": "Incorrect API key provided.", "type": "invalid_api_key"}})


@app.get("/")
@app.get("/v1")
def root():
    return {
        "service": "KeepingYouAwake LiteLLM Proxy",
        "version": "2.0",
        "pipeline": "dialect → litellm → cursor-auto → dialect",
        "models": MODEL_ALIASES,
        "endpoints": ["/v1/models", "/v1/chat/completions", "/v1/messages", "/v1/status"],
    }


@app.get("/v1/status")
@app.get("/status")
@app.get("/health")
def status():
    return {"status": "ok", "serverPort": PORT, "engine": "litellm+cursor-agent"}


@app.get("/v1/models")
@app.get("/models")
def list_models(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
):
    check_auth(authorization, x_api_key)
    return {
        "object": "list",
        "data": [
            {
                "id": mid,
                "object": "model",
                "created": 1700000000,
                "owned_by": "cursor",
                "permission": [],
            }
            for mid in MODEL_ALIASES
        ],
    }


@app.get("/v1/models/{model_id:path}")
def get_model(
    model_id: str,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
):
    check_auth(authorization, x_api_key)
    return {
        "id": model_id,
        "object": "model",
        "created": 1700000000,
        "owned_by": "cursor",
        "permission": [],
    }


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
@app.post("/v1/completions")
@app.post("/completions")
async def chat_completions(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    user_agent: Optional[str] = Header(default=None, alias="user-agent"),
):
    check_auth(authorization, x_api_key)
    body = await request.json()
    stream = bool(body.get("stream"))
    messages = body.get("messages")
    if not messages and body.get("prompt"):
        messages = [{"role": "user", "content": body["prompt"]}]
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages required"}})

    dialect = detect_client_dialect(
        user_agent=user_agent,
        path=str(request.url.path),
        extra_headers=dict(request.headers),
    )
    _CLIENT_DIALECT.set(dialect)
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    _CLIENT_TOOLS.set(tools)
    # If the client advertises tools, act as LLM backend and return native tool_calls.
    # Otherwise keep agent-behind-API for plain chat.
    if tools:
        _EXEC_MODE.set("llm")
        echo_model = body.get("model") or "cursor-auto"
        print(f"[kya_llm_proxy] chat dialect={dialect} ua={user_agent!r} tools={len(tools)}", flush=True)
        try:
            prompt = messages_to_prompt(list(messages))
            raw_text = run_cursor_agent_with_quota_retry(prompt)
            prose, tool_calls = extract_tool_calls_for_dialect(raw_text, tools, dialect)
            if tool_calls:
                print(
                    f"[kya_llm_proxy] {dialect}: mapped {len(tool_calls)} OpenAI tool_calls",
                    flush=True,
                )
            payload = openai_chat_payload(
                model=echo_model, prose=prose, tool_calls=tool_calls
            )
            if stream:
                # Minimal SSE: one assistant delta then finish
                def oai_sse():
                    chunk = {
                        "id": payload["id"],
                        "object": "chat.completion.chunk",
                        "created": payload["created"],
                        "model": echo_model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": payload["choices"][0]["message"],
                                "finish_reason": payload["choices"][0]["finish_reason"],
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(oai_sse(), media_type="text/event-stream")
            return JSONResponse(payload)
        except Exception as e:
            raise HTTPException(
                status_code=502, detail={"error": {"message": str(e), "type": "proxy_error"}}
            ) from e

    _EXEC_MODE.set("agent")
    echo_model = body.get("model") or "cursor-auto"
    print(f"[kya_llm_proxy] chat dialect={dialect} mode=agent ua={user_agent!r}", flush=True)
    try:
        if stream:
            gen = completion(
                model=UPSTREAM_MODEL,
                messages=messages,
                stream=True,
                custom_llm_provider="kya_cursor",
            )

            def openai_sse():
                for chunk in gen:
                    # LiteLLM returns ModelResponse-like chunks; re-echo requested model id
                    try:
                        if hasattr(chunk, "model"):
                            chunk.model = echo_model
                        data = chunk.model_dump_json() if hasattr(chunk, "model_dump_json") else json.dumps(chunk)
                    except Exception:
                        data = chunk if isinstance(chunk, str) else json.dumps(chunk, default=str)
                    yield f"data: {data}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(openai_sse(), media_type="text/event-stream")

        resp = completion(
            model=UPSTREAM_MODEL,
            messages=messages,
            stream=False,
            custom_llm_provider="kya_cursor",
        )
        if hasattr(resp, "model"):
            resp.model = echo_model
        payload = json.loads(resp.model_dump_json()) if hasattr(resp, "model_dump_json") else resp
        return JSONResponse(payload)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": {"message": str(e), "type": "proxy_error"}}) from e


@app.post("/v1/messages")
@app.post("/messages")
async def anthropic_messages(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    user_agent: Optional[str] = Header(default=None, alias="user-agent"),
    anthropic_version: Optional[str] = Header(default=None, alias="anthropic-version"),
):
    check_auth(authorization, x_api_key)
    body = await request.json()
    stream = bool(body.get("stream"))
    messages = list(body.get("messages") or [])
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    dialect = detect_client_dialect(
        user_agent=user_agent,
        path=str(request.url.path),
        anthropic_version=anthropic_version,
        extra_headers=dict(request.headers),
    )
    # /v1/messages is always Anthropic wire format on the way out.
    if dialect not in ("claude",):
        # UA said something else but hit Anthropic path — still speak Anthropic on the wire.
        print(
            f"[kya_llm_proxy] messages path with dialect hint={dialect}; forcing claude wire format",
            flush=True,
        )
        dialect = "claude"
    _CLIENT_DIALECT.set(dialect)
    _CLIENT_TOOLS.set(tools)
    # Claude Code owns tools — nested cursor-agent tools = multi-minute stalls.
    _EXEC_MODE.set("llm")
    echo_model = body.get("model") or "cursor-auto"
    print(
        f"[kya_llm_proxy] messages dialect={dialect} ua={user_agent!r} tools={len(tools)}",
        flush=True,
    )

    sys_prompt = body.get("system")
    prompt_messages = messages
    if sys_prompt is not None:
        prompt_messages = [{"role": "system", "content": sys_prompt}, *messages]

    try:
        prompt = messages_to_prompt(prompt_messages)
        if stream:
            return StreamingResponse(
                iter_anthropic_sse_live(
                    prompt, model=echo_model, tools=tools, dialect=dialect
                ),
                media_type="text/event-stream",
            )
        raw_text = run_cursor_agent_with_quota_retry(prompt)
        prose, tool_calls = extract_tool_calls_for_dialect(raw_text, tools, dialect)
        if looks_like_incomplete_assistant_turn(prose, tool_calls) and tools:
            print(
                "[kya_llm_proxy] claude: incomplete turn (narration, no tools); nudging once",
                flush=True,
            )
            nudged = run_cursor_agent_with_quota_retry(incomplete_turn_nudge(prompt))
            if nudged:
                raw_text = f"{raw_text}\n{nudged}".strip()
                prose, tool_calls = extract_tool_calls_for_dialect(raw_text, tools, dialect)
        if tool_calls:
            print(
                f"[kya_llm_proxy] claude: mapped {len(tool_calls)} Anthropic tool_use",
                flush=True,
            )
        payload = anthropic_message_payload(
            model=echo_model, prose=prose, tool_calls=tool_calls
        )
        return JSONResponse(payload)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={"type": "error", "error": {"type": "api_error", "message": str(e)}},
        ) from e


@app.post("/v1/responses")
@app.post("/responses")
async def openai_responses(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
):
    """Map Responses API → chat.completions via LiteLLM-shaped messages."""
    check_auth(authorization, x_api_key)
    body = await request.json()
    stream = bool(body.get("stream"))
    raw_input = body.get("input") or body.get("messages") or ""
    messages: list[dict[str, Any]] = []
    if isinstance(raw_input, str):
        messages = [{"role": "user", "content": raw_input}]
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                messages.append(
                    {
                        "role": item.get("role") or "user",
                        "content": flatten_content(item.get("content") or item.get("text")),
                    }
                )
    # reuse chat completions path by building openai completion then wrapping
    echo_model = body.get("model") or "cursor-auto"
    try:
        if stream:
            gen = completion(
                model=UPSTREAM_MODEL,
                messages=messages,
                stream=True,
                custom_llm_provider="kya_cursor",
            )

            def resp_sse():
                text_acc = []
                for chunk in gen:
                    delta = None
                    try:
                        delta = chunk.choices[0].delta.content
                    except Exception:
                        delta = None
                    if delta:
                        text_acc.append(delta)
                        yield f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': delta})}\n\n"
                full = "".join(text_acc)
                yield f"data: {json.dumps({'type': 'response.output_text.done', 'text': full})}\n\n"
                yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': 'resp_litellm', 'model': echo_model, 'status': 'completed'}})}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(resp_sse(), media_type="text/event-stream")

        resp = completion(
            model=UPSTREAM_MODEL,
            messages=messages,
            stream=False,
            custom_llm_provider="kya_cursor",
        )
        text = resp.choices[0].message.content or ""
        return JSONResponse(
            {
                "id": getattr(resp, "id", "resp_litellm"),
                "object": "response",
                "created": int(time.time()),
                "model": echo_model,
                "status": "completed",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    }
                ],
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            }
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": {"message": str(e), "type": "proxy_error"}}) from e


@app.options("/{full_path:path}")
async def options_cors(full_path: str):
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, x-api-key, anthropic-version, anthropic-beta",
        },
    )


@app.middleware("http")
async def add_cors(request: Request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


def main() -> None:
    # ready marker for ObjC waiter
    print(f"[kya_llm_proxy] starting on http://{HOST}:{PORT} (litellm+cursor-agent)", flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
