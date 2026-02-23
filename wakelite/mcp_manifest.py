from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List

from .config import CLAUDE_MCP_CONFIG, CODEX_CONFIG, MCP_MANIFEST_FILE, MCP_HTTP_PORT, ensure_dirs
from .mcp_server import TOOLS


def generate_manifest(mcp_command: str) -> Dict:
    ensure_dirs()
    manifest = {
        "version": "v1",
        "name": "wakelite",
        "description": "WakeLite reliability-first wake + timer scheduler",
        "transports": {
            "stdio": {"command": mcp_command, "args": []},
            "http": {"url": f"http://127.0.0.1:{MCP_HTTP_PORT}/mcp"},
        },
        "tools": TOOLS,
    }
    MCP_MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    MCP_MANIFEST_FILE.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _upsert_claude_global(mcp_command: str) -> None:
    data = {"mcpServers": {}}
    if CLAUDE_MCP_CONFIG.exists():
        try:
            data = json.loads(CLAUDE_MCP_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            data = {"mcpServers": {}}
    if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
        data["mcpServers"] = {}

    data["mcpServers"]["wakelite"] = {
        "command": mcp_command,
        "args": [],
    }

    CLAUDE_MCP_CONFIG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _upsert_codex_global(mcp_command: str) -> None:
    CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    content = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""

    if "[mcp_servers.wakelite]" in content:
        # Basic in-place rewrite of this section is intentionally conservative.
        lines = content.splitlines()
        out: List[str] = []
        skip = False
        for i, line in enumerate(lines):
            if line.strip() == "[mcp_servers.wakelite]":
                skip = True
                out.append("[mcp_servers.wakelite]")
                out.append(f'command = "{mcp_command}"')
                out.append("args = []")
                continue
            if skip and line.startswith("[") and line.endswith("]"):
                skip = False
            if not skip:
                out.append(line)
        content = "\n".join(out).rstrip() + "\n"
    else:
        if content and not content.endswith("\n"):
            content += "\n"
        content += "\n[mcp_servers.wakelite]\n"
        content += f'command = "{mcp_command}"\n'
        content += "args = []\n"

    CODEX_CONFIG.write_text(content, encoding="utf-8")


def install_global_configs(mcp_command: str, targets: Iterable[str]) -> Dict[str, bool]:
    result = {"claude": False, "codex": False}
    tset = {t.strip().lower() for t in targets}

    if "claude" in tset:
        _upsert_claude_global(mcp_command)
        result["claude"] = True

    if "codex" in tset:
        _upsert_codex_global(mcp_command)
        result["codex"] = True

    return result


def manual_snippets(mcp_command: str) -> Dict[str, str]:
    claude_snippet = json.dumps(
        {
            "mcpServers": {
                "wakelite": {
                    "command": mcp_command,
                    "args": [],
                }
            }
        },
        indent=2,
    )

    codex_snippet = "\n".join(
        [
            "[mcp_servers.wakelite]",
            f'command = "{mcp_command}"',
            "args = []",
        ]
    )

    return {
        "claude": claude_snippet,
        "codex": codex_snippet,
    }
