"""Event service: calendar event storage backed by JSON-LD (schema.org/Event)."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.cron.types import CronPayload, CronSchedule, EventRecord, EventStore

if TYPE_CHECKING:
    from nanobot.cron.service import CronService

# Maps internal status values to schema.org EventStatus terms.
_STATUS_TO_SCHEMA = {
    "confirmed": "EventScheduled",
    "tentative": "EventPostponed",
    "cancelled": "EventCancelled",
}
_SCHEMA_TO_STATUS = {v: k for k, v in _STATUS_TO_SCHEMA.items()}

# JSON-LD @context included in every serialised event.
_JSONLD_CONTEXT = {
    "@vocab": "https://schema.org/",
    "nanobot": "https://nanobot.ai/schema#",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


class EventService:
    """Manage calendar events stored as JSON-LD.

    Events are persisted to *store_path* as a JSON file whose ``events``
    array contains schema.org/Event objects.  When an event has a payload
    a linked ``CronJob`` is created via *cron_service* so the execution
    machinery remains entirely inside the existing :class:`CronService`.
    """

    def __init__(self, store_path: Path, cron_service: CronService | None = None):
        self.store_path = store_path
        self._cron = cron_service
        self._store: EventStore | None = None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_store(self) -> EventStore:
        if self._store is not None:
            return self._store

        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                events = [self._from_jsonld(e) for e in data.get("events", [])]
                self._store = EventStore(version=data.get("version", 1), events=events)
            except Exception as exc:
                logger.warning("Failed to load event store: {}", exc)
                self._store = EventStore()
        else:
            self._store = EventStore()

        return self._store

    def _save_store(self) -> None:
        if not self._store:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": self._store.version,
            "events": [self._to_jsonld(e) for e in self._store.events],
        }
        self.store_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # JSON-LD serialisation
    # ------------------------------------------------------------------

    def _to_jsonld(self, event: EventRecord) -> dict:
        d: dict = {
            "@context": _JSONLD_CONTEXT,
            "@type": "Event",
            "identifier": event.id,
            "name": event.name,
            "startDate": _ms_to_iso(event.start_ms),
            "eventStatus": _STATUS_TO_SCHEMA.get(event.status, "EventScheduled"),
            "nanobot:createdAtMs": event.created_at_ms,
            "nanobot:updatedAtMs": event.updated_at_ms,
        }
        if event.description:
            d["description"] = event.description
        if event.end_ms is not None:
            d["endDate"] = _ms_to_iso(event.end_ms)
        if event.location:
            d["location"] = event.location
        if event.job_id:
            d["nanobot:jobId"] = event.job_id
        if event.payload:
            d["nanobot:payload"] = {
                "kind": event.payload.kind,
                "message": event.payload.message,
                "deliver": event.payload.deliver,
                "channel": event.payload.channel,
                "to": event.payload.to,
            }
        return d

    def _from_jsonld(self, data: dict) -> EventRecord:
        payload: CronPayload | None = None
        if "nanobot:payload" in data:
            p = data["nanobot:payload"]
            payload = CronPayload(
                kind=p.get("kind", "agent_turn"),
                message=p.get("message", ""),
                deliver=p.get("deliver", False),
                channel=p.get("channel"),
                to=p.get("to"),
            )

        end_ms: int | None = None
        if "endDate" in data:
            try:
                end_ms = _iso_to_ms(data["endDate"])
            except Exception:
                pass

        return EventRecord(
            id=data.get("identifier", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            start_ms=_iso_to_ms(data["startDate"]),
            end_ms=end_ms,
            location=data.get("location"),
            status=_SCHEMA_TO_STATUS.get(data.get("eventStatus", "EventScheduled"), "confirmed"),
            job_id=data.get("nanobot:jobId"),
            payload=payload,
            created_at_ms=data.get("nanobot:createdAtMs", 0),
            updated_at_ms=data.get("nanobot:updatedAtMs", 0),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_event(
        self,
        name: str,
        start_ms: int,
        end_ms: int | None = None,
        description: str = "",
        location: str | None = None,
        status: str = "confirmed",
        payload: CronPayload | None = None,
        alarm_ms: int | None = None,
    ) -> EventRecord:
        """Add a calendar event.

        If *payload* is provided a linked ``CronJob`` is created.  The job
        fires at *alarm_ms* when given (e.g. a VALARM offset), otherwise at
        *start_ms*.
        """
        store = self._load_store()
        now = _now_ms()
        event = EventRecord(
            id=str(uuid.uuid4())[:8],
            name=name,
            start_ms=start_ms,
            end_ms=end_ms,
            description=description,
            location=location,
            status=status,
            payload=payload,
            created_at_ms=now,
            updated_at_ms=now,
        )

        if payload and self._cron:
            trigger_ms = alarm_ms if alarm_ms is not None else start_ms
            schedule = CronSchedule(kind="at", at_ms=trigger_ms)
            job = self._cron.add_job(
                name=name[:30],
                schedule=schedule,
                message=payload.message,
                deliver=payload.deliver,
                channel=payload.channel,
                to=payload.to,
                delete_after_run=True,
            )
            event.job_id = job.id

        store.events.append(event)
        self._save_store()
        logger.info("Event: added '{}' ({})", name, event.id)
        return event

    def list_events(self, upcoming_only: bool = True) -> list[EventRecord]:
        """Return events sorted by start time, optionally filtering past ones."""
        store = self._load_store()
        now = _now_ms()
        events = store.events
        if upcoming_only:
            events = [e for e in events if e.start_ms >= now]
        return sorted(events, key=lambda e: e.start_ms)

    def get_event(self, event_id: str) -> EventRecord | None:
        """Look up a single event by id."""
        store = self._load_store()
        return next((e for e in store.events if e.id == event_id), None)

    def remove_event(self, event_id: str) -> bool:
        """Remove an event and its linked CronJob if present."""
        store = self._load_store()
        event = next((e for e in store.events if e.id == event_id), None)
        if not event:
            return False

        if event.job_id and self._cron:
            self._cron.remove_job(event.job_id)

        store.events = [e for e in store.events if e.id != event_id]
        self._save_store()
        logger.info("Event: removed {}", event_id)
        return True

    def import_ics(self, ics_text: str) -> list[EventRecord]:
        """Parse an ICS/iCalendar string and import all VEVENT components.

        Requires the optional ``icalendar`` package::

            pip install nanobot-ai[ics]

        A linked ``CronJob`` is created for any VEVENT that contains a
        VALARM so the agent is notified at alarm time.
        """
        try:
            from icalendar import Calendar
        except ImportError:
            raise RuntimeError(
                "The 'icalendar' package is required for ICS import. "
                "Install it with: pip install nanobot-ai[ics]"
            ) from None

        cal = Calendar.from_ical(ics_text)
        imported: list[EventRecord] = []

        for component in cal.walk():
            if component.name != "VEVENT":
                continue

            name = str(component.get("SUMMARY", "Untitled event"))
            description = str(component.get("DESCRIPTION", ""))
            raw_location = component.get("LOCATION")
            location = str(raw_location) if raw_location else None

            # Parse start time (may be a date or datetime)
            dtstart = component.get("DTSTART")
            if not dtstart:
                continue
            start_val = dtstart.dt
            if hasattr(start_val, "timestamp"):
                start_ms = int(start_val.timestamp() * 1000)
            else:
                # date-only – treat as midnight UTC
                start_ms = int(
                    datetime(start_val.year, start_val.month, start_val.day,
                             tzinfo=timezone.utc).timestamp() * 1000
                )

            # Parse end time
            end_ms: int | None = None
            dtend = component.get("DTEND")
            if dtend:
                end_val = dtend.dt
                if hasattr(end_val, "timestamp"):
                    end_ms = int(end_val.timestamp() * 1000)

            # Status
            ical_status = str(component.get("STATUS", "CONFIRMED")).upper()
            status = {"CONFIRMED": "confirmed", "TENTATIVE": "tentative",
                      "CANCELLED": "cancelled"}.get(ical_status, "confirmed")

            # Check for VALARM → execution payload
            payload: CronPayload | None = None
            alarm_ms: int | None = None
            for sub in component.walk():
                if sub.name != "VALARM":
                    continue
                trigger = sub.get("TRIGGER")
                if not trigger:
                    continue
                trigger_val = trigger.dt
                if hasattr(trigger_val, "total_seconds"):
                    # Duration relative to DTSTART
                    from datetime import timezone as _tz
                    base = datetime.fromtimestamp(start_ms / 1000, tz=_tz.utc)
                    alarm_dt = base + trigger_val
                    alarm_ms = int(alarm_dt.timestamp() * 1000)
                elif hasattr(trigger_val, "timestamp"):
                    alarm_ms = int(trigger_val.timestamp() * 1000)

                alarm_desc = str(sub.get("DESCRIPTION", name))
                payload = CronPayload(
                    kind="agent_turn",
                    message=f"Event reminder: {alarm_desc}",
                    deliver=False,
                )
                break  # use first alarm only

            event = self.add_event(
                name=name,
                start_ms=start_ms,
                end_ms=end_ms,
                description=description,
                location=location,
                status=status,
                payload=payload,
                alarm_ms=alarm_ms,
            )
            imported.append(event)

        logger.info("Event: imported {} event(s) from ICS", len(imported))
        return imported
