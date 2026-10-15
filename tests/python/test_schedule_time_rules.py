from datetime import datetime

import pytest

from backend.app.schedules.time_rules import due_occurrences, next_occurrence, prepare_trigger


def epoch(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def test_after_uses_received_instant_and_has_no_second_occurrence():
    trigger = prepare_trigger("after", "1d2h3m4s", None, epoch("2026-10-16T00:00:00Z"))

    assert trigger.kind == "after"
    assert trigger.timezone == "Asia/Shanghai"
    assert trigger.next_at == epoch("2026-10-17T02:03:04Z")
    assert next_occurrence(trigger, trigger.next_at) is None


def test_every_uses_elapsed_seconds_and_keeps_original_anchor_after_clock_changes():
    trigger = prepare_trigger("every", "1h", "America/New_York", epoch("2026-03-08T06:30:00Z"))

    assert trigger.interval_seconds == 3600
    assert trigger.next_at == epoch("2026-03-08T07:30:00Z")
    assert next_occurrence(trigger, epoch("2026-03-08T09:30:00Z")) == epoch("2026-03-08T10:30:00Z")
    assert next_occurrence(trigger, epoch("2026-03-08T09:29:59.999Z")) == epoch("2026-03-08T09:30:00Z")
    assert next_occurrence(trigger, epoch("2026-03-08T06:00:00Z")) == trigger.next_at


@pytest.mark.parametrize("when", ["0s", "366d", "365d1s", "-1s", "0.5s", "1m1h", "1h1h", "", "1w"])
def test_duration_rejects_invalid_or_unbounded_values(when):
    with pytest.raises(ValueError):
        prepare_trigger("after", when, None, epoch("2026-10-16T00:00:00Z"))


@pytest.mark.parametrize("when, expected", [("1s", 1), ("365d", 31536000), ("1d 2h 3m 4s", 93784)])
def test_duration_accepts_inclusive_limits_and_readable_combinations(when, expected):
    trigger = prepare_trigger("every", when, None, 0)
    assert trigger.next_at == expected


@pytest.mark.parametrize("kind, zone", [("sometimes", "UTC"), ("after", "Not/AZone"), ("after", "")])
def test_unknown_trigger_or_timezone_is_rejected(kind, zone):
    with pytest.raises(ValueError):
        prepare_trigger(kind, "1s", zone, 0)


@pytest.mark.parametrize("when, zone, received, expected", [
    ("2026-10-16T09:30:00", None, "2026-10-16T00:00:00Z", "2026-10-16T01:30:00Z"),
    ("2026-10-16T09:30:00+08:00", None, "2026-10-16T00:00:00Z", "2026-10-16T01:30:00Z"),
    ("2026-10-16T01:30:00Z", "UTC", "2026-10-16T00:00:00Z", "2026-10-16T01:30:00Z"),
    ("09:30", None, "2026-10-16T01:00:00Z", "2026-10-16T01:30:00Z"),
    ("09:30", None, "2026-10-16T01:30:00Z", "2026-10-17T01:30:00Z"),
    ("2027-10-16T00:00:00Z", "UTC", "2026-10-16T00:00:00Z", "2027-10-16T00:00:00Z"),
    ("2026-10-16T00:00:00Z", "UTC", "2026-10-16T00:00:00Z", "2026-10-16T00:00:00Z"),
])
def test_at_resolves_explicit_and_daily_local_times(when, zone, received, expected):
    trigger = prepare_trigger("at", when, zone, epoch(received))
    assert trigger.next_at == epoch(expected)
    assert next_occurrence(trigger, trigger.next_at) is None


@pytest.mark.parametrize("when, zone", [
    ("2026-10-15T23:59:59Z", "UTC"),
    ("2027-10-16T00:00:01Z", "UTC"),
    ("2026-10-16T09:30:00+09:00", "Asia/Shanghai"),
    ("2026-10-16", "UTC"),
    ("24:00", "UTC"),
    ("12:60", "UTC"),
])
def test_at_rejects_past_out_of_range_or_mismatched_zone(when, zone):
    with pytest.raises(ValueError):
        prepare_trigger("at", when, zone, epoch("2026-10-16T00:00:00Z"))


@pytest.mark.parametrize("when, received", [
    ("2026-03-08T02:30:00", "2026-03-08T00:00:00Z"),
    ("02:30", "2026-03-08T05:00:00Z"),
    ("2026-11-01T01:30:00", "2026-11-01T00:00:00Z"),
    ("01:30", "2026-11-01T04:00:00Z"),
    ("2026-03-08T02:30:00-05:00", "2026-03-08T00:00:00Z"),
])
def test_at_rejects_dst_gaps_and_requires_offset_for_ambiguous_local_time(when, received):
    with pytest.raises(ValueError):
        prepare_trigger("at", when, "America/New_York", epoch(received))


@pytest.mark.parametrize("offset, expected", [("-04:00", "2026-11-01T05:30:00Z"), ("-05:00", "2026-11-01T06:30:00Z")])
def test_at_explicit_offset_selects_either_fall_back_instant(offset, expected):
    trigger = prepare_trigger("at", f"2026-11-01T01:30:00{offset}", "America/New_York", epoch("2026-11-01T00:00:00Z"))
    assert trigger.next_at == epoch(expected)


@pytest.mark.parametrize("kind, when, expected", [
    ("cron", "30 9 * * *", "2026-10-16T09:30:00Z"),
    ("every", "30 9 * * *", "2026-10-16T09:30:00Z"),
    ("cron", "15 30 9 * * *", "2026-10-16T09:30:15Z"),
    ("cron", "*/20 9-10 * * MON-FRI", "2026-10-16T09:00:00Z"),
    ("cron", "5/20 9 * * *", "2026-10-16T09:05:00Z"),
    ("cron", "0 0 * * 0", "2026-10-18T00:00:00Z"),
    ("cron", "0 0 * * 7", "2026-10-18T00:00:00Z"),
    ("cron", "0 0 * * sun", "2026-10-18T00:00:00Z"),
    ("cron", "0 0 1 * mon", "2027-02-01T00:00:00Z"),
    ("cron", "0,30 0,12 * * *", "2026-10-16T00:30:00Z"),
])
def test_cron_calendar_fields_and_every_alias(kind, when, expected):
    trigger = prepare_trigger(kind, when, "UTC", epoch("2026-10-16T00:00:00Z"))

    assert trigger.kind == "cron"
    assert trigger.cron == when
    assert trigger.interval_seconds is None
    assert trigger.next_at == epoch(expected)


def test_cron_repeats_strictly_after_current_instant_and_preserves_creation_floor():
    trigger = prepare_trigger("cron", "* * * * * *", "UTC", epoch("2026-10-16T00:00:00.5Z"))
    assert trigger.next_at == epoch("2026-10-16T00:00:01Z")
    assert next_occurrence(trigger, epoch("2026-10-16T00:00:01Z")) == epoch("2026-10-16T00:00:02Z")
    assert next_occurrence(trigger, epoch("2026-10-15T00:00:00Z")) == trigger.next_at


def test_cron_can_skip_to_leap_day_without_scanning_each_second():
    trigger = prepare_trigger("cron", "0 0 0 29 2 *", "UTC", epoch("2028-03-01T00:00:00Z"))
    assert trigger.next_at == epoch("2032-02-29T00:00:00Z")


@pytest.mark.parametrize("weekday, received, first, following", [
    ("tue", "2026-01-01T00:00:00Z", "2028-02-29T00:00:00Z", "2056-02-29T00:00:00Z"),
    ("mon", "2042-01-01T00:00:00Z", "2044-02-29T00:00:00Z", "2072-02-29T00:00:00Z"),
])
def test_accepted_sparse_cron_retains_successor_beyond_five_years(weekday, received, first, following):
    trigger = prepare_trigger("cron", f"0 0 29 2 {weekday}", "UTC", epoch(received))

    assert trigger.next_at == epoch(first)
    assert next_occurrence(trigger, trigger.next_at) == epoch(following)
    assert due_occurrences(trigger, trigger.next_at, trigger.next_at) == (
        1, epoch(first), epoch(following),
    )


@pytest.mark.parametrize("zone, first", [
    ("UTC", "9999-12-31T00:00:00Z"),
    ("Asia/Shanghai", "9999-12-30T16:00:00Z"),
    ("America/New_York", "9999-12-31T05:00:00Z"),
])
def test_cron_clips_search_at_datetime_upper_bound(zone, first):
    trigger = prepare_trigger("cron", "0 0 31 12 *", zone, epoch("9998-12-31T12:00:00Z"))

    assert trigger.next_at == epoch(first)
    assert next_occurrence(trigger, trigger.next_at) is None
    assert due_occurrences(trigger, trigger.next_at, trigger.next_at) == (1, epoch(first), None)


@pytest.mark.parametrize("zone, received, last", [
    ("UTC", "9999-12-31T23:59:58Z", "9999-12-31T23:59:59Z"),
    ("Asia/Shanghai", "9999-12-31T15:59:58Z", "9999-12-31T15:59:59Z"),
    ("America/New_York", "9999-12-31T23:59:58Z", "9999-12-31T23:59:59Z"),
])
def test_cron_counts_last_representable_slot_and_rejects_creation_after_it(zone, received, last):
    trigger = prepare_trigger("cron", "* * * * * *", zone, epoch(received))

    assert trigger.next_at == epoch(last)
    assert next_occurrence(trigger, trigger.next_at) is None
    assert due_occurrences(trigger, trigger.next_at, epoch("9999-12-31T23:59:59Z")) == (
        1, epoch(last), None,
    )
    with pytest.raises(ValueError, match="five years"):
        prepare_trigger("cron", "* * * * * *", zone, epoch(last))


@pytest.mark.parametrize("when", [
    "* * * *", "* * * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *",
    "* * * 13 *", "* * * * 8", "*/0 * * * *", "3-1 * * * *", "1,,2 * * * *",
    "* * * * monday", "0 0 31 2 *", "0 0 31 4 *", "1h", "* * * * * * garbage",
])
def test_cron_rejects_invalid_fields_and_impossible_dates(when):
    with pytest.raises(ValueError):
        prepare_trigger("cron", when, "UTC", epoch("2026-10-16T00:00:00Z"))


def test_cron_skips_spring_gap_and_only_uses_first_fall_back_slot():
    spring = prepare_trigger("cron", "30 2 * * *", "America/New_York", epoch("2026-03-08T05:00:00Z"))
    assert spring.next_at == epoch("2026-03-09T06:30:00Z")

    fall = prepare_trigger("cron", "30 1 * * *", "America/New_York", epoch("2026-11-01T04:00:00Z"))
    assert fall.next_at == epoch("2026-11-01T05:30:00Z")
    assert next_occurrence(fall, epoch("2026-11-01T05:30:00Z")) == epoch("2026-11-02T06:30:00Z")
    assert next_occurrence(fall, epoch("2026-11-01T06:00:00Z")) == epoch("2026-11-02T06:30:00Z")


def test_cron_rejects_valid_calendar_expression_without_match_in_five_years():
    with pytest.raises(ValueError, match="five years"):
        prepare_trigger("cron", "0 0 29 2 mon", "UTC", epoch("2028-03-01T00:00:00Z"))


@pytest.mark.parametrize("host_zone", ["UTC", "America/Los_Angeles", "Asia/Tokyo"])
def test_selected_zone_is_independent_of_host_timezone(monkeypatch, host_zone):
    monkeypatch.setenv("TZ", host_zone)
    received = epoch("2026-10-16T00:00:00Z")
    assert prepare_trigger("at", "09:30", None, received).next_at == epoch("2026-10-16T01:30:00Z")
    assert prepare_trigger("cron", "30 9 * * *", None, received).next_at == epoch("2026-10-16T01:30:00Z")


@pytest.mark.parametrize("received", [float("nan"), float("inf"), float("-inf"), True, 1e100])
def test_non_finite_or_unrepresentable_host_instants_are_rejected(received):
    with pytest.raises(ValueError):
        prepare_trigger("after", "1s", None, received)


@pytest.mark.parametrize("when", [None, True, "1" * 201 + "s"])
def test_time_expression_must_be_bounded_text(when):
    with pytest.raises(ValueError):
        prepare_trigger("after", when, None, 0)


@pytest.mark.parametrize("kind, zone", [(None, "UTC"), ([], "UTC"), ("after", False)])
def test_trigger_and_zone_must_be_text(kind, zone):
    with pytest.raises(ValueError):
        prepare_trigger(kind, "1s", zone, 0)


@pytest.mark.parametrize("limit, expected", [(None, (4, 40, 50)), (3, (3, 30, None)), (4, (4, 40, None)), (5, (4, 40, 50))])
def test_due_intervals_count_from_persisted_first_and_obey_remaining_runs(limit, expected):
    trigger = prepare_trigger("every", "10s", "UTC", 0)
    assert due_occurrences(trigger, 10, 49.99, limit) == expected


def test_single_due_occurrence_has_no_future_slot():
    trigger = prepare_trigger("after", "10s", "UTC", 0)
    assert due_occurrences(trigger, 10, 100) == (1, 10, None)


@pytest.mark.parametrize("first, until, limit", [(10, 9, None), (10, float("nan"), None), (10, 20, 0), (10, 20, True), (10, 20, 1001)])
def test_due_occurrences_rejects_invalid_range_and_run_limit(first, until, limit):
    trigger = prepare_trigger("every", "10s", "UTC", 0)
    with pytest.raises(ValueError):
        due_occurrences(trigger, first, until, limit)


def test_due_cron_counts_five_years_of_seconds_without_enumerating_each_slot():
    trigger = prepare_trigger("cron", "* * * * * *", "UTC", epoch("2025-12-31T23:59:59Z"))
    until = epoch("2030-12-31T23:59:59Z")
    assert due_occurrences(trigger, trigger.next_at, until) == (157766400, until, epoch("2031-01-01T00:00:00Z"))
    assert due_occurrences(trigger, trigger.next_at, until, 1000) == (1000, epoch("2026-01-01T00:16:39Z"), None)


@pytest.mark.parametrize("limit, count, latest, following", [
    (None, 25, "2026-01-02T09:15:30Z", "2026-01-02T09:30:00Z"),
    (11, 11, "2026-01-01T17:30:30Z", None),
])
def test_due_cron_counts_cartesian_times_from_persisted_middle_of_day(limit, count, latest, following):
    trigger = prepare_trigger("cron", "0,30 0,15,30,45 1,9,17 * * *", "UTC", epoch("2026-01-01T00:00:00Z"))
    expected = (count, epoch(latest), None if following is None else epoch(following))
    assert due_occurrences(trigger, epoch("2026-01-01T09:15:30Z"), epoch("2026-01-02T09:15:30.9Z"), limit) == expected


@pytest.mark.parametrize("zone, first, until, count, latest, following", [
    ("America/New_York", "2026-03-08T05:00:00Z", "2026-03-09T03:59:59Z", 82800, "2026-03-09T03:59:59Z", "2026-03-09T04:00:00Z"),
    ("America/New_York", "2026-11-01T04:00:00Z", "2026-11-02T04:59:59Z", 86400, "2026-11-02T04:59:59Z", "2026-11-02T05:00:00Z"),
    ("America/New_York", "2026-11-01T04:00:00Z", "2026-11-01T06:15:00Z", 7200, "2026-11-01T05:59:59Z", "2026-11-01T07:00:00Z"),
    ("Australia/Lord_Howe", "2026-10-03T13:30:00Z", "2026-10-04T12:59:59Z", 84600, "2026-10-04T12:59:59Z", "2026-10-04T13:00:00Z"),
])
def test_due_cron_skips_gap_and_counts_only_fold_zero(zone, first, until, count, latest, following):
    trigger = prepare_trigger("cron", "* * * * * *", zone, epoch(first) - 1)
    assert due_occurrences(trigger, epoch(first), epoch(until)) == (count, epoch(latest), epoch(following))
