"""Memory system for persistent agent memory."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph (2-5 sentences) summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


_KAIZEN_SCAN_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_kaizen_candidates",
            "description": (
                "Save task automation candidates identified in the conversation to KAIZEN.md. "
                "Call with an empty list if no candidates are found."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Descriptions of repeatable tasks that could be automated as "
                            "scripts or skills to reduce token usage, prevent failures, "
                            "and speed up future runs. Empty list if none found."
                        ),
                    }
                },
                "required": ["candidates"],
            },
        },
    }
]


_KAIZEN_REVIEW_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "select_kaizen_tasks",
            "description": (
                "Select up to 3 highest-priority automation candidates from KAIZEN.md "
                "to convert into skills or scripts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selected_tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Up to 3 task descriptions to convert into skills or scripts, "
                            "ordered by expected impact (most impactful first)."
                        ),
                        "maxItems": 3,
                    }
                },
                "required": ["selected_tasks"],
            },
        },
    }
]


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self.kaizen_file = self.memory_dir / "KAIZEN.md"
        self._kaizen_last_review_file = self.memory_dir / ".kaizen_last_review"

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    # ------------------------------------------------------------------
    # Kaizen helpers
    # ------------------------------------------------------------------

    def read_kaizen(self) -> str:
        """Return the current contents of KAIZEN.md (empty string if absent)."""
        if self.kaizen_file.exists():
            return self.kaizen_file.read_text(encoding="utf-8")
        return ""

    def append_kaizen(self, candidates: list[str]) -> None:
        """Append automation candidates to KAIZEN.md with a timestamp header."""
        if not candidates:
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [f"\n## {timestamp}\n"]
        for c in candidates:
            lines.append(f"- {c}")
        with open(self.kaizen_file, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def should_run_kaizen_review(self, interval_days: int) -> bool:
        """Return True when KAIZEN.md exists and the review interval has elapsed."""
        if not self.kaizen_file.exists() or not self.read_kaizen().strip():
            return False
        if not self._kaizen_last_review_file.exists():
            return True
        try:
            last = datetime.fromisoformat(
                self._kaizen_last_review_file.read_text(encoding="utf-8").strip()
            )
            return datetime.now() - last >= timedelta(days=interval_days)
        except (ValueError, OSError):
            return True

    def _update_kaizen_last_review(self) -> None:
        self._kaizen_last_review_file.write_text(
            datetime.now().isoformat(), encoding="utf-8"
        )

    async def kaizen_scan(
        self,
        provider: LLMProvider,
        model: str,
        conversation_lines: list[str],
    ) -> bool:
        """Scan a conversation for scriptable task candidates and append them to KAIZEN.md.

        Returns True on success (including no candidates found), False on failure.
        """
        if not conversation_lines:
            return True

        prompt = (
            "Review this conversation and identify any repeatable tasks that could be "
            "automated as scripts or skills to reduce token usage, prevent failures, "
            "and speed up future runs. Call save_kaizen_candidates with your findings "
            "(use an empty list if nothing qualifies).\n\n"
            "## Conversation\n"
            + "\n".join(conversation_lines)
        )
        try:
            response = await provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a process improvement agent. "
                            "Call save_kaizen_candidates with any automation candidates found."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=_KAIZEN_SCAN_TOOL,
                model=model,
            )

            if not response.has_tool_calls:
                logger.debug("Kaizen scan: LLM did not call save_kaizen_candidates, skipping")
                return True

            args = response.tool_calls[0].arguments
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(args, dict):
                return True

            candidates = args.get("candidates", [])
            if isinstance(candidates, list) and candidates:
                self.append_kaizen([str(c) for c in candidates if c])
                logger.info("Kaizen scan: {} candidate(s) added to KAIZEN.md", len(candidates))
            return True
        except Exception:
            logger.exception("Kaizen scan failed")
            return False

    async def kaizen_review(
        self,
        provider: LLMProvider,
        model: str,
    ) -> list[str]:
        """Review KAIZEN.md and return up to 3 top-priority task descriptions to automate.

        Records the review timestamp so the caller can enforce the daily interval.
        Returns an empty list when KAIZEN.md is empty or the LLM finds nothing to select.
        """
        kaizen_content = self.read_kaizen()
        if not kaizen_content.strip():
            return []

        prompt = (
            "Review the following automation candidates from KAIZEN.md and select up to 3 "
            "that would provide the most value as skills or scripts (most tokens saved, "
            "highest reliability improvement, most reuse). "
            "Call select_kaizen_tasks with your selection.\n\n"
            f"## KAIZEN.md\n{kaizen_content}"
        )
        try:
            response = await provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a process improvement agent. "
                            "Call select_kaizen_tasks with the top candidates to automate."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=_KAIZEN_REVIEW_TOOL,
                model=model,
            )

            if not response.has_tool_calls:
                logger.debug("Kaizen review: LLM did not call select_kaizen_tasks")
                self._update_kaizen_last_review()
                return []

            args = response.tool_calls[0].arguments
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(args, dict):
                return []

            selected = args.get("selected_tasks", [])
            tasks: list[str] = []
            if isinstance(selected, list):
                tasks = [str(t) for t in selected if t][:3]

            self._update_kaizen_last_review()
            logger.info("Kaizen review: {} task(s) selected for conversion", len(tasks))
            return tasks
        except Exception:
            logger.exception("Kaizen review failed")
            return []

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """Consolidate old messages into MEMORY.md + HISTORY.md via LLM tool call.

        Returns True on success (including no-op), False on failure.
        """
        if archive_all:
            old_messages = session.messages
            keep_count = 0
            logger.info("Memory consolidation (archive_all): {} messages", len(session.messages))
        else:
            keep_count = memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages:
                return True
            logger.info("Memory consolidation: {} to consolidate, {} keep", len(old_messages), keep_count)

        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}")

        current_memory = self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{chr(10).join(lines)}"""

        try:
            response = await provider.chat(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_MEMORY_TOOL,
                model=model,
            )

            if not response.has_tool_calls:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                return False

            args = response.tool_calls[0].arguments
            # Some providers return arguments as a JSON string instead of dict
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(args, dict):
                logger.warning("Memory consolidation: unexpected arguments type {}", type(args).__name__)
                return False

            if entry := args.get("history_entry"):
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                self.append_history(entry)
            if update := args.get("memory_update"):
                if not isinstance(update, str):
                    update = json.dumps(update, ensure_ascii=False)
                if update != current_memory:
                    self.write_long_term(update)

            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info("Memory consolidation done: {} messages, last_consolidated={}", len(session.messages), session.last_consolidated)

            if not archive_all and lines:
                await self.kaizen_scan(provider, model, lines)

            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False
