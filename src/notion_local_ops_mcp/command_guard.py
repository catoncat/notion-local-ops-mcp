from __future__ import annotations

import re
from dataclasses import dataclass


SUPPORTED_COMMAND_GUARD_MODES = {"off", "warn", "block"}

NETWORK_RE = re.compile(
    r"(https?://|\bcurl\b|\bwget\b|\bnc\b|\bnetcat\b|\bssh\b|\bscp\b|\bftp\b)",
    re.IGNORECASE,
)
DESTRUCTIVE_RE = re.compile(
    r"(^|\s)(sudo|su|chmod\s+-R|chown\s+-R|mkfs|mount|umount|find\b[^;&|]*\s-delete\b|"
    r"git\b[^;&|]*\breset\s+--hard\b|git\b[^;&|]*\bclean\s+-[^\s]*[fx][^\s]*|"
    r"rm\s+-[^\s]*r[^\s]*f|rm\s+-[^\s]*f[^\s]*r)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CommandGuardDecision:
    mode: str
    allowed: bool
    warnings: list[dict[str, str]]


def normalize_command_guard_mode(mode: str | None) -> str:
    normalized = (mode or "off").strip().lower() or "off"
    if normalized not in SUPPORTED_COMMAND_GUARD_MODES:
        return "off"
    return normalized


def evaluate_command(command: str, *, mode: str | None) -> CommandGuardDecision:
    normalized_mode = normalize_command_guard_mode(mode)
    if normalized_mode == "off":
        return CommandGuardDecision(mode=normalized_mode, allowed=True, warnings=[])

    warnings: list[dict[str, str]] = []
    compact = " ".join(command.split())
    if DESTRUCTIVE_RE.search(compact):
        warnings.append(
            {
                "code": "destructive_command",
                "message": "Command looks destructive.",
            }
        )
    if NETWORK_RE.search(compact):
        warnings.append(
            {
                "code": "network_command",
                "message": "Command appears to use network access.",
            }
        )

    allowed = normalized_mode != "block" or not warnings
    return CommandGuardDecision(
        mode=normalized_mode,
        allowed=allowed,
        warnings=warnings,
    )
