"""Events tool: manage calendar events with execution payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.cron.event_service import EventService
from nanobot.cron.types import CronPayload


class EventsTool(Tool):
    """Tool to manage scheduled calendar events.

    Events are stored as JSON-LD (schema.org/Event) and can be imported from
    ICS/iCalendar data.  When a payload is attached, a CronJob fires at the
    event start (or alarm) time to trigger an agent turn.

    Actions: add, list, get, remove, import_ics.
    """

    def __init__(self, event_service: EventService):
        self._events = event_service
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context used for event delivery."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "events"

    @property
    def description(self) -> str:
        return (
            "Manage calendar events for future execution. "
            "Actions: add, list, get, remove, import_ics. "
            "Events are stored as JSON-LD and can optionally trigger an agent turn at event time."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "get", "remove", "import_ics"],
                    "description": "Action to perform",
                },
                "name": {
                    "type": "string",
                    "description": "Event title (for add)",
                },
                "start": {
                    "type": "string",
                    "description": "Event start as ISO datetime, e.g. '2026-03-15T10:00:00' (for add)",
                },
                "end": {
                    "type": "string",
                    "description": "Event end as ISO datetime (optional, for add)",
                },
                "description": {
                    "type": "string",
                    "description": "Event description (optional, for add)",
                },
                "location": {
                    "type": "string",
                    "description": "Event location (optional, for add)",
                },
                "reminder_message": {
                    "type": "string",
                    "description": (
                        "If set, a CronJob fires at event time (or reminder_min before) "
                        "with this message as the agent prompt (for add)"
                    ),
                },
                "reminder_min": {
                    "type": "integer",
                    "description": (
                        "Minutes before event start to trigger the reminder. "
                        "Defaults to 0 (fire at start time). Only used when reminder_message is set."
                    ),
                },
                "event_id": {
                    "type": "string",
                    "description": "Event ID (for get, remove)",
                },
                "upcoming_only": {
                    "type": "boolean",
                    "description": "Only list future events (default true, for list)",
                },
                "ics_text": {
                    "type": "string",
                    "description": "Raw ICS/iCalendar text to import (for import_ics)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        name: str = "",
        start: str = "",
        end: str = "",
        description: str = "",
        location: str = "",
        reminder_message: str = "",
        reminder_min: int = 0,
        event_id: str = "",
        upcoming_only: bool = True,
        ics_text: str = "",
        **kwargs: Any,
    ) -> str:
        if action == "add":
            return self._add(name, start, end, description, location,
                             reminder_message, reminder_min)
        if action == "list":
            return self._list(upcoming_only)
        if action == "get":
            return self._get(event_id)
        if action == "remove":
            return self._remove(event_id)
        if action == "import_ics":
            return self._import_ics(ics_text)
        return f"Unknown action: {action}"

    # ------------------------------------------------------------------

    def _add(
        self,
        name: str,
        start: str,
        end: str,
        description: str,
        location: str,
        reminder_message: str,
        reminder_min: int,
    ) -> str:
        if not name:
            return "Error: name is required for add"
        if not start:
            return "Error: start datetime is required for add"

        try:
            start_ms = int(datetime.fromisoformat(start).timestamp() * 1000)
        except ValueError:
            return f"Error: invalid start datetime '{start}' – use ISO format e.g. '2026-03-15T10:00:00'"

        end_ms: int | None = None
        if end:
            try:
                end_ms = int(datetime.fromisoformat(end).timestamp() * 1000)
            except ValueError:
                return f"Error: invalid end datetime '{end}' – use ISO format"

        payload: CronPayload | None = None
        alarm_ms: int | None = None
        if reminder_message:
            if not self._channel or not self._chat_id:
                return "Error: no session context (channel/chat_id) for reminder delivery"
            payload = CronPayload(
                kind="agent_turn",
                message=reminder_message,
                deliver=True,
                channel=self._channel,
                to=self._chat_id,
            )
            alarm_ms = start_ms - (reminder_min * 60 * 1000)

        event = self._events.add_event(
            name=name,
            start_ms=start_ms,
            end_ms=end_ms,
            description=description,
            location=location or None,
            payload=payload,
            alarm_ms=alarm_ms,
        )

        parts = [f"Added event '{event.name}' (id: {event.id})"]
        if event.job_id:
            parts.append(f"reminder job: {event.job_id}")
        return ", ".join(parts)

    def _list(self, upcoming_only: bool) -> str:
        events = self._events.list_events(upcoming_only=upcoming_only)
        if not events:
            return "No events found."

        from nanobot.cron.event_service import _ms_to_iso
        lines = []
        for e in events:
            start = _ms_to_iso(e.start_ms)
            line = f"- [{e.id}] {e.name} @ {start}"
            if e.location:
                line += f" ({e.location})"
            if e.job_id:
                line += f" [reminder job: {e.job_id}]"
            lines.append(line)
        label = "Upcoming events" if upcoming_only else "All events"
        return f"{label}:\n" + "\n".join(lines)

    def _get(self, event_id: str) -> str:
        if not event_id:
            return "Error: event_id is required for get"
        event = self._events.get_event(event_id)
        if not event:
            return f"Event {event_id} not found"

        from nanobot.cron.event_service import _ms_to_iso
        parts = [
            f"id: {event.id}",
            f"name: {event.name}",
            f"status: {event.status}",
            f"start: {_ms_to_iso(event.start_ms)}",
        ]
        if event.end_ms:
            parts.append(f"end: {_ms_to_iso(event.end_ms)}")
        if event.description:
            parts.append(f"description: {event.description}")
        if event.location:
            parts.append(f"location: {event.location}")
        if event.job_id:
            parts.append(f"reminder job: {event.job_id}")
        return "\n".join(parts)

    def _remove(self, event_id: str) -> str:
        if not event_id:
            return "Error: event_id is required for remove"
        if self._events.remove_event(event_id):
            return f"Removed event {event_id}"
        return f"Event {event_id} not found"

    def _import_ics(self, ics_text: str) -> str:
        if not ics_text.strip():
            return "Error: ics_text is required for import_ics"
        try:
            imported = self._events.import_ics(ics_text)
        except RuntimeError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            return f"Error parsing ICS: {exc}"

        if not imported:
            return "No events found in ICS data"
        lines = [f"- [{e.id}] {e.name}" for e in imported]
        return f"Imported {len(imported)} event(s):\n" + "\n".join(lines)
