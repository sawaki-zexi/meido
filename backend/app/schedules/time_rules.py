"""Pure schedule time rules; all instants are UTC Unix timestamps."""

from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
import calendar
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Asia/Shanghai"
MAX_DURATION_SECONDS = 365 * 24 * 60 * 60
MAX_CRON_SEARCH_YEARS = 5
MAX_WHEN_LENGTH = 200
_WEEKDAYS = {name: value for value, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}


@dataclass(frozen=True)
class Trigger:
    kind: str
    when: str
    timezone: str
    next_at: float
    interval_seconds: float | None = None
    cron: str | None = None


def prepare_trigger(
    trigger: str, when: str, timezone: str | None, received_at: float
) -> Trigger:
    """Resolve the first nominal instant using the host's received_at, never wall-clock now."""
    if not isinstance(trigger, str) or trigger not in {"at", "after", "every", "cron"}:
        raise ValueError("unknown schedule trigger")
    if not isinstance(when, str) or not when.strip() or len(when) > MAX_WHEN_LENGTH:
        raise ValueError("when must be nonempty text of at most 200 characters")
    _utc(received_at)
    zone_name = DEFAULT_TIMEZONE if timezone is None else timezone
    if not isinstance(zone_name, str):
        raise ValueError("timezone must be a valid IANA zone")
    try:
        zone = ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("timezone must be a valid IANA zone") from error
    if trigger == "at":
        return Trigger(trigger, when, zone_name, _at(when.strip(), zone, received_at))
    if trigger == "cron" or trigger == "every" and len(when.split()) in (5, 6):
        expression = " ".join(when.split())
        return Trigger("cron", when, zone_name, _cron_next(expression, zone, received_at), cron=expression)
    match = re.fullmatch(r"(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?", when.strip())
    if match is None:
        raise ValueError("invalid duration")
    seconds = sum(int(value or 0) * unit for value, unit in zip(match.groups(), (86400, 3600, 60, 1)))
    if not 1 <= seconds <= MAX_DURATION_SECONDS:
        raise ValueError("duration must be between 1 second and 365 days")
    return Trigger(
        trigger, when, zone_name, received_at + seconds,
        interval_seconds=float(seconds) if trigger == "every" else None,
    )


def next_occurrence(trigger: Trigger, after: float) -> float | None:
    """Return the first later nominal instant, retaining the original creation floor."""
    _utc(after)
    if trigger.kind == "every" and trigger.interval_seconds is not None:
        steps = max(0, math.floor((after - trigger.next_at) / trigger.interval_seconds) + 1)
        return trigger.next_at + steps * trigger.interval_seconds
    if trigger.kind == "cron" and trigger.cron is not None:
        if trigger.next_at > after:
            return trigger.next_at
        return _cron_next(trigger.cron, ZoneInfo(trigger.timezone), after)
    return None


def due_occurrences(
    trigger: Trigger, first: float, until: float, limit: int | None = None
) -> tuple[int, float, float | None]:
    """Count due nominal slots from persisted first, bounded by remaining runs."""
    _utc(first)
    _utc(until)
    if until < first:
        raise ValueError("until must not precede first")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000):
        raise ValueError("limit must be an integer from 1 to 1000")
    if trigger.kind in {"at", "after"}:
        return 1, first, None
    if trigger.kind == "every" and trigger.interval_seconds is not None:
        count = math.floor((until - first) / trigger.interval_seconds) + 1
        if limit is not None:
            count = min(count, limit)
        latest = first + (count - 1) * trigger.interval_seconds
        following = None if count == limit else first + count * trigger.interval_seconds
        return count, latest, following
    if trigger.kind == "cron" and trigger.cron is not None:
        return _cron_due(trigger, first, until, limit)
    raise ValueError("invalid recurring trigger")


def _utc(instant: float) -> datetime:
    if isinstance(instant, bool) or not isinstance(instant, (int, float)):
        raise ValueError("instant must be a finite UTC timestamp")
    try:
        if not math.isfinite(instant):
            raise ValueError("instant must be a finite UTC timestamp")
        return datetime.fromtimestamp(instant, UTC)
    except (OverflowError, OSError) as error:
        raise ValueError("instant is outside the supported datetime range") from error


def _local_instants(local: datetime, zone: ZoneInfo) -> list[float]:
    """Validate wall time by UTC round trip, distinguishing gaps and folds."""
    instants: list[float] = []
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold).timestamp()
        round_trip = datetime.fromtimestamp(candidate, UTC).astimezone(zone)
        if round_trip.replace(tzinfo=None) == local and candidate not in instants:
            instants.append(candidate)
    return instants


def _at(when: str, zone: ZoneInfo, received_at: float) -> float:
    clock = re.fullmatch(r"(\d{1,2}):(\d{2})", when)
    if clock is not None:
        received_local = datetime.fromtimestamp(received_at, UTC).astimezone(zone).replace(tzinfo=None)
        local = received_local.replace(hour=int(clock[1]), minute=int(clock[2]), second=0, microsecond=0)
        if local <= received_local:
            local += timedelta(days=1)
        parsed = local
    else:
        if not re.match(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}", when):
            raise ValueError("at requires an ISO datetime or HH:MM")
        parsed = datetime.fromisoformat(when)
    if parsed.tzinfo is not None:
        resolved = parsed.astimezone(zone)
        if resolved.utcoffset() != parsed.utcoffset() or resolved.replace(tzinfo=None) != parsed.replace(tzinfo=None):
            raise ValueError("ISO offset does not match the selected timezone")
        instant = parsed.timestamp()
    else:
        instants = _local_instants(parsed, zone)
        if not instants:
            raise ValueError("local time does not exist; provide an ISO datetime with a valid offset")
        if len(instants) > 1:
            raise ValueError("local time is ambiguous; provide an ISO datetime with an explicit offset")
        instant = instants[0]
    if not received_at <= instant <= received_at + MAX_DURATION_SECONDS:
        raise ValueError("at must be between received_at and 365 days later")
    return instant


@dataclass(frozen=True)
class _Cron:
    seconds: tuple[int, ...]
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: tuple[int, ...]
    months: tuple[int, ...]
    weekdays: tuple[int, ...]


def _wall_rank(rule: _Cron, second: int) -> int:
    """Number of selected Cartesian wall times at or before second of day."""
    if second < 0:
        return 0
    hour, rest = divmod(second, 3600)
    minute, sec = divmod(rest, 60)
    count = bisect_left(rule.hours, hour) * len(rule.minutes) * len(rule.seconds)
    if hour in rule.hours:
        count += bisect_left(rule.minutes, minute) * len(rule.seconds)
        if minute in rule.minutes:
            count += bisect_right(rule.seconds, sec)
    return count


def _wall_second(rule: _Cron, index: int) -> int:
    hour_index, rest = divmod(index, len(rule.minutes) * len(rule.seconds))
    minute_index, second_index = divmod(rest, len(rule.seconds))
    return rule.hours[hour_index] * 3600 + rule.minutes[minute_index] * 60 + rule.seconds[second_index]


def _wall_segments(
    midnight: datetime, zone: ZoneInfo, lower: int, upper: int
) -> Iterator[tuple[int, int, int]]:
    """Split a local hour at offset/gap boundaries; folded slots always use fold=0."""
    def offset(second: int) -> int | None:
        local = midnight + timedelta(seconds=second)
        aware = local.replace(tzinfo=zone, fold=0)
        if datetime.fromtimestamp(aware.timestamp(), UTC).astimezone(zone).replace(tzinfo=None) != local:
            return None
        delta = aware.utcoffset()
        assert delta is not None
        return int(delta.total_seconds())

    start_offset, end_offset = offset(lower), offset(upper)
    if start_offset == end_offset:
        if start_offset is not None:
            yield lower, upper, start_offset
        return
    middle = (lower + upper) // 2
    yield from _wall_segments(midnight, zone, lower, middle)
    yield from _wall_segments(midnight, zone, middle + 1, upper)


def _cron_due(trigger: Trigger, first: float, until: float, limit: int | None) -> tuple[int, float, float | None]:
    assert trigger.cron is not None
    rule = _parse_cron(trigger.cron)
    zone = ZoneInfo(trigger.timezone)
    day = _utc(first).astimezone(zone).date()
    last_day = _utc(until).astimezone(zone).date()
    count, latest = 0, first
    while day <= last_day:
        if day.month in rule.months and day.day in rule.days and (day.weekday() + 1) % 7 in rule.weekdays:
            midnight = datetime(day.year, day.month, day.day)
            utc_base = midnight.replace(tzinfo=UTC).timestamp()
            for hour in rule.hours:
                for lower, upper, offset in _wall_segments(midnight, zone, hour * 3600, hour * 3600 + 3599):
                    lower = max(lower, math.ceil(first - utc_base + offset))
                    upper = min(upper, math.floor(until - utc_base + offset))
                    if lower > upper:
                        continue
                    start_rank = _wall_rank(rule, lower - 1)
                    available = _wall_rank(rule, upper) - start_rank
                    taken = available if limit is None else min(available, limit - count)
                    if not taken:
                        continue
                    latest = utc_base + _wall_second(rule, start_rank + taken - 1) - offset
                    count += taken
                    if count == limit:
                        return count, latest, None
        day += timedelta(days=1)
    if not count:
        raise ValueError("first must be a valid nominal cron slot")
    return count, latest, next_occurrence(trigger, until)


def _cron_field(text: str, minimum: int, maximum: int, *, weekday: bool = False) -> tuple[int, ...]:
    def number(atom: str) -> int:
        if weekday and atom.lower() in _WEEKDAYS:
            value = _WEEKDAYS[atom.lower()]
        elif re.fullmatch(r"[0-9]+", atom):
            value = int(atom)
        else:
            raise ValueError("invalid cron field")
        if not minimum <= value <= maximum:
            raise ValueError("cron field is outside its allowed range")
        return value

    values: set[int] = set()
    for part in text.split(","):
        pieces = part.split("/")
        if len(pieces) > 2 or len(pieces) == 2 and not re.fullmatch(r"[0-9]+", pieces[1]):
            raise ValueError("invalid cron step")
        step = int(pieces[1]) if len(pieces) == 2 else 1
        if step < 1:
            raise ValueError("cron step must be positive")
        base = pieces[0]
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            bounds = base.split("-")
            if len(bounds) != 2:
                raise ValueError("invalid cron range")
            start, end = number(bounds[0]), number(bounds[1])
        else:
            start = number(base)
            end = maximum if len(pieces) == 2 else start
        if start > end:
            raise ValueError("cron ranges must be ascending")
        values.update(range(start, end + 1, step))
    return tuple(sorted({value % 7 for value in values} if weekday else values))


@lru_cache(maxsize=256)
def _parse_cron(expression: str) -> _Cron:
    fields = expression.split()
    if len(fields) == 5:
        fields.insert(0, "0")
    if len(fields) != 6:
        raise ValueError("cron requires five or six fields, with seconds first")
    return _Cron(
        _cron_field(fields[0], 0, 59),
        _cron_field(fields[1], 0, 59),
        _cron_field(fields[2], 0, 23),
        _cron_field(fields[3], 1, 31),
        _cron_field(fields[4], 1, 12),
        _cron_field(fields[5], 0, 7, weekday=True),
    )


def _cron_next(expression: str, zone: ZoneInfo, after: float) -> float:
    """Search calendar days, then allowed hours/minutes/seconds, for at most five years."""
    rule = _parse_cron(expression)
    utc_after = datetime.fromtimestamp(after, UTC)
    end_year = utc_after.year + MAX_CRON_SEARCH_YEARS
    end_day = min(utc_after.day, calendar.monthrange(end_year, utc_after.month)[1])
    horizon = utc_after.replace(year=end_year, day=end_day)
    local_after = utc_after.astimezone(zone)
    day = local_after.date()
    last_day = horizon.astimezone(zone).date()
    while day <= last_day:
        if day.month in rule.months and day.day in rule.days and (day.weekday() + 1) % 7 in rule.weekdays:
            floor = (local_after.hour, local_after.minute, local_after.second) if day == local_after.date() else (-1, -1, -1)
            for hour in rule.hours:
                if hour < floor[0]:
                    continue
                for minute in rule.minutes:
                    if hour == floor[0] and minute < floor[1]:
                        continue
                    for second in rule.seconds:
                        if (hour, minute, second) <= floor:
                            continue
                        local = datetime(day.year, day.month, day.day, hour, minute, second)
                        # fold=0 is the first occurrence of an ambiguous local slot.
                        candidate = local.replace(tzinfo=zone, fold=0).timestamp()
                        round_trip = datetime.fromtimestamp(candidate, UTC).astimezone(zone)
                        if round_trip.replace(tzinfo=None) != local:
                            continue  # Spring gap, including zones that skipped an entire date.
                        if after < candidate <= horizon.timestamp():
                            return candidate
        day += timedelta(days=1)
    raise ValueError("cron has no matching occurrence within five years")
