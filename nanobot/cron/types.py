"""Cron types."""

from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class CronSchedule:
    """Schedule definition for a cron job."""
    kind: Literal["at", "every", "cron"]
    # For "at": timestamp in ms
    at_ms: int | None = None
    # For "every": interval in ms
    every_ms: int | None = None
    # For "cron": cron expression (e.g. "0 9 * * *")
    expr: str | None = None
    # Timezone for cron expressions
    tz: str | None = None


@dataclass
class CronPayload:
    """What to do when the job runs."""
    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    # Deliver response to channel
    deliver: bool = False
    channel: str | None = None  # e.g. "whatsapp"
    to: str | None = None  # e.g. phone number


@dataclass
class CronJobState:
    """Runtime state of a job."""
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None


@dataclass
class CronJob:
    """A scheduled job."""
    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False


@dataclass
class CronStore:
    """Persistent store for cron jobs."""
    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)


@dataclass
class EventRecord:
    """A calendar event stored in JSON-LD (schema.org/Event) format.

    Separates rich calendar metadata from cron execution machinery.
    When ``payload`` is set a linked ``CronJob`` is created automatically
    and its id is kept in ``job_id`` so the two stay in sync.
    """
    id: str
    name: str
    start_ms: int                                               # schema.org: startDate
    description: str = ""                                      # schema.org: description
    end_ms: Optional[int] = None                               # schema.org: endDate
    location: Optional[str] = None                             # schema.org: location
    status: Literal["confirmed", "tentative", "cancelled"] = "confirmed"
    # Execution linkage – populated when a CronJob backs this event
    job_id: Optional[str] = None
    payload: Optional[CronPayload] = None
    created_at_ms: int = 0
    updated_at_ms: int = 0


@dataclass
class EventStore:
    """Persistent store for calendar events (JSON-LD serialised)."""
    version: int = 1
    events: list[EventRecord] = field(default_factory=list)
