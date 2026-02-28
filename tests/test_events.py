"""Tests for EventService and EventsTool."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.cron.event_service import EventService, _ms_to_iso, _iso_to_ms
from nanobot.cron.types import CronPayload, EventRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _future_ms(offset_s: int = 3600) -> int:
    return int(time.time() * 1000) + offset_s * 1000


def _make_service(tmp_path: Path, cron_service=None) -> EventService:
    return EventService(tmp_path / "cron" / "events.json", cron_service=cron_service)


# ---------------------------------------------------------------------------
# Unit tests – EventService
# ---------------------------------------------------------------------------

def test_add_and_list_event(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    start_ms = _future_ms()

    event = svc.add_event(name="Team sync", start_ms=start_ms)

    assert event.id
    assert event.name == "Team sync"
    assert event.start_ms == start_ms
    assert event.status == "confirmed"
    assert event.job_id is None  # no payload → no linked job

    listed = svc.list_events()
    assert len(listed) == 1
    assert listed[0].id == event.id


def test_event_persisted_as_jsonld(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    start_ms = _future_ms()
    svc.add_event(name="Persisted event", start_ms=start_ms, description="desc", location="Room 1")

    store_path = tmp_path / "cron" / "events.json"
    assert store_path.exists()
    data = json.loads(store_path.read_text())

    assert data["version"] == 1
    assert len(data["events"]) == 1

    e = data["events"][0]
    assert e["@type"] == "Event"
    assert e["@context"]["@vocab"] == "https://schema.org/"
    assert e["name"] == "Persisted event"
    assert e["description"] == "desc"
    assert e["location"] == "Room 1"
    assert e["eventStatus"] == "EventScheduled"
    assert "startDate" in e


def test_jsonld_roundtrip(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    start_ms = _future_ms()
    end_ms = start_ms + 3600_000

    original = svc.add_event(
        name="Roundtrip",
        start_ms=start_ms,
        end_ms=end_ms,
        description="A description",
        location="London",
        status="tentative",
    )

    # Force reload from disk
    svc2 = _make_service(tmp_path)
    events = svc2.list_events(upcoming_only=False)

    assert len(events) == 1
    r = events[0]
    assert r.id == original.id
    assert r.name == "Roundtrip"
    assert r.start_ms == start_ms
    assert r.end_ms == end_ms
    assert r.description == "A description"
    assert r.location == "London"
    assert r.status == "tentative"


def test_get_event(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    event = svc.add_event(name="Fetch me", start_ms=_future_ms())

    found = svc.get_event(event.id)
    assert found is not None
    assert found.id == event.id

    assert svc.get_event("nonexistent") is None


def test_remove_event(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    event = svc.add_event(name="Remove me", start_ms=_future_ms())

    assert svc.remove_event(event.id) is True
    assert svc.list_events(upcoming_only=False) == []
    assert svc.remove_event(event.id) is False  # already gone


def test_remove_event_cancels_linked_job(tmp_path: Path) -> None:
    mock_cron = MagicMock()
    mock_cron.add_job.return_value = MagicMock(id="job123")

    svc = _make_service(tmp_path, cron_service=mock_cron)
    payload = CronPayload(kind="agent_turn", message="Reminder", deliver=False)
    event = svc.add_event(name="With job", start_ms=_future_ms(), payload=payload)

    assert event.job_id == "job123"
    svc.remove_event(event.id)
    mock_cron.remove_job.assert_called_once_with("job123")


def test_add_event_creates_cron_job_for_payload(tmp_path: Path) -> None:
    mock_cron = MagicMock()
    mock_cron.add_job.return_value = MagicMock(id="abc12345")

    svc = _make_service(tmp_path, cron_service=mock_cron)
    start_ms = _future_ms()
    payload = CronPayload(kind="agent_turn", message="Go time", deliver=True, channel="cli", to="user")

    event = svc.add_event(name="Triggered event", start_ms=start_ms, payload=payload)

    assert event.job_id == "abc12345"
    mock_cron.add_job.assert_called_once()
    call_kwargs = mock_cron.add_job.call_args
    assert call_kwargs.kwargs["message"] == "Go time"


def test_add_event_alarm_offset(tmp_path: Path) -> None:
    mock_cron = MagicMock()
    mock_cron.add_job.return_value = MagicMock(id="alarm1")

    svc = _make_service(tmp_path, cron_service=mock_cron)
    start_ms = _future_ms(7200)  # 2 hours from now
    alarm_ms = start_ms - 15 * 60 * 1000  # 15 min before

    payload = CronPayload(kind="agent_turn", message="Heads up", deliver=False)
    svc.add_event(name="Alarm event", start_ms=start_ms, payload=payload, alarm_ms=alarm_ms)

    call_kwargs = mock_cron.add_job.call_args
    schedule = call_kwargs.kwargs["schedule"]
    assert schedule.at_ms == alarm_ms


def test_list_events_upcoming_only(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)

    past_ms = int(time.time() * 1000) - 3600_000  # 1 hour ago
    future_ms = _future_ms()

    svc.add_event(name="Past", start_ms=past_ms)
    svc.add_event(name="Future", start_ms=future_ms)

    upcoming = svc.list_events(upcoming_only=True)
    assert len(upcoming) == 1
    assert upcoming[0].name == "Future"

    all_events = svc.list_events(upcoming_only=False)
    assert len(all_events) == 2


def test_list_events_sorted_by_start(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    now = int(time.time() * 1000)

    svc.add_event(name="Third", start_ms=now + 3_000_000)
    svc.add_event(name="First", start_ms=now + 1_000_000)
    svc.add_event(name="Second", start_ms=now + 2_000_000)

    listed = svc.list_events()
    names = [e.name for e in listed]
    assert names == ["First", "Second", "Third"]


# ---------------------------------------------------------------------------
# ICS import tests (require icalendar package)
# ---------------------------------------------------------------------------

icalendar = pytest.importorskip("icalendar", reason="icalendar not installed")


_SIMPLE_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
UID:test-event-1@example.com
SUMMARY:Sprint planning
DTSTART:20260315T100000Z
DTEND:20260315T110000Z
LOCATION:Conference Room A
DESCRIPTION:Quarterly sprint planning session
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
"""

_ICS_WITH_ALARM = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
UID:test-alarm-1@example.com
SUMMARY:Dentist appointment
DTSTART:20260401T090000Z
DTEND:20260401T100000Z
BEGIN:VALARM
TRIGGER:-PT15M
ACTION:DISPLAY
DESCRIPTION:Dentist in 15 minutes
END:VALARM
END:VEVENT
END:VCALENDAR
"""

_ICS_MULTIPLE = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:e1@x.com
SUMMARY:Event One
DTSTART:20260310T120000Z
END:VEVENT
BEGIN:VEVENT
UID:e2@x.com
SUMMARY:Event Two
DTSTART:20260311T120000Z
STATUS:TENTATIVE
END:VEVENT
END:VCALENDAR
"""


def test_import_ics_basic(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    imported = svc.import_ics(_SIMPLE_ICS)

    assert len(imported) == 1
    e = imported[0]
    assert e.name == "Sprint planning"
    assert e.location == "Conference Room A"
    assert e.description == "Quarterly sprint planning session"
    assert e.status == "confirmed"
    assert e.job_id is None  # no alarm → no linked job

    # Verify startDate
    start_dt = datetime.fromtimestamp(e.start_ms / 1000, tz=timezone.utc)
    assert start_dt.year == 2026
    assert start_dt.month == 3
    assert start_dt.day == 15


def test_import_ics_with_alarm_creates_job(tmp_path: Path) -> None:
    mock_cron = MagicMock()
    mock_cron.add_job.return_value = MagicMock(id="alarm_job")

    svc = _make_service(tmp_path, cron_service=mock_cron)
    imported = svc.import_ics(_ICS_WITH_ALARM)

    assert len(imported) == 1
    e = imported[0]
    assert e.name == "Dentist appointment"
    assert e.job_id == "alarm_job"

    # The alarm is TRIGGER:-PT15M (15 min before 09:00 UTC = 08:45 UTC)
    call_kwargs = mock_cron.add_job.call_args
    alarm_schedule = call_kwargs.kwargs["schedule"]
    alarm_dt = datetime.fromtimestamp(alarm_schedule.at_ms / 1000, tz=timezone.utc)
    assert alarm_dt.hour == 8
    assert alarm_dt.minute == 45


def test_import_ics_multiple_events(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    imported = svc.import_ics(_ICS_MULTIPLE)

    assert len(imported) == 2
    names = {e.name for e in imported}
    assert names == {"Event One", "Event Two"}

    statuses = {e.name: e.status for e in imported}
    assert statuses["Event One"] == "confirmed"
    assert statuses["Event Two"] == "tentative"


def test_import_ics_persisted(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    svc.import_ics(_SIMPLE_ICS)

    # Reload from disk
    svc2 = _make_service(tmp_path)
    events = svc2.list_events(upcoming_only=False)
    assert len(events) == 1
    assert events[0].name == "Sprint planning"


def test_import_ics_missing_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    real_import = builtins.__import__

    def _block_icalendar(name, *args, **kwargs):
        if name == "icalendar":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_icalendar)
    svc = _make_service(tmp_path)
    with pytest.raises(RuntimeError, match="icalendar"):
        svc.import_ics(_SIMPLE_ICS)


# ---------------------------------------------------------------------------
# EventsTool tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_events_tool_add_and_list(tmp_path: Path) -> None:
    from nanobot.agent.tools.events import EventsTool

    svc = _make_service(tmp_path)
    tool = EventsTool(svc)
    tool.set_context("cli", "user123")

    start = "2026-06-01T14:00:00+00:00"
    result = await tool.execute(action="add", name="Board meeting", start=start)
    assert "Board meeting" in result

    result = await tool.execute(action="list")
    assert "Board meeting" in result


@pytest.mark.asyncio
async def test_events_tool_get_and_remove(tmp_path: Path) -> None:
    from nanobot.agent.tools.events import EventsTool

    svc = _make_service(tmp_path)
    tool = EventsTool(svc)
    tool.set_context("cli", "user123")

    await tool.execute(action="add", name="Standup", start="2026-07-01T09:00:00+00:00")
    events = svc.list_events(upcoming_only=False)
    eid = events[0].id

    result = await tool.execute(action="get", event_id=eid)
    assert "Standup" in result

    result = await tool.execute(action="remove", event_id=eid)
    assert "Removed" in result

    assert svc.list_events(upcoming_only=False) == []


@pytest.mark.asyncio
async def test_events_tool_invalid_start(tmp_path: Path) -> None:
    from nanobot.agent.tools.events import EventsTool

    svc = _make_service(tmp_path)
    tool = EventsTool(svc)

    result = await tool.execute(action="add", name="Bad", start="not-a-date")
    assert "Error" in result


@pytest.mark.asyncio
async def test_events_tool_import_ics(tmp_path: Path) -> None:
    from nanobot.agent.tools.events import EventsTool

    svc = _make_service(tmp_path)
    tool = EventsTool(svc)
    tool.set_context("cli", "user")

    result = await tool.execute(action="import_ics", ics_text=_SIMPLE_ICS)
    assert "Sprint planning" in result
    assert "Imported 1" in result
