"""AdaptiveThrottle: milestone/heartbeat edits and the budget shared by concurrent tasks."""

import pytest

import app.bot.telegram_bot as tb
from app.bot.handlers import job_cards
from app.services.edit_throttle import AT_KEY, PCT_KEY, PHASE_KEY, AdaptiveThrottle


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def throttle(clock):
    return AdaptiveThrottle(
        min_interval=4.0, per_task=1.5, max_interval=15.0, milestone=5.0, idle_after=60.0, clock=clock
    )


# ------------------------------------------------------------ intervals


def test_interval_has_a_floor_and_grows_with_tasks(throttle):
    assert throttle.interval(0) == 4.0
    assert throttle.interval(1) == 4.0
    assert throttle.interval(2) == 4.0
    assert throttle.interval(3) == 4.5
    assert throttle.interval(10) == 15.0
    assert throttle.interval(20) == 30.0


def test_heartbeat_never_below_interval(throttle):
    assert throttle.heartbeat(1) == 15.0
    assert throttle.heartbeat(10) == 15.0
    assert throttle.heartbeat(20) == 30.0


def test_active_count_drops_idle_messages(throttle, clock):
    throttle.touch("a")
    clock.advance(30)
    throttle.touch("b")
    assert throttle.active_count() == 2
    clock.advance(31)  # "a" idle for 61 s
    assert throttle.active_count() == 1
    throttle.forget("b")
    throttle.forget("missing")
    assert throttle.active_count() == 0


def test_interval_without_argument_uses_live_count(throttle):
    for key in range(6):
        throttle.touch(key)
    assert throttle.interval() == 9.0


# ------------------------------------------------------------ milestones


def test_first_update_always_passes_and_is_recorded(throttle, clock):
    state = {}
    assert throttle.due(state, "a", 1.0, phase="downloading")
    assert state == {AT_KEY: clock.now, PCT_KEY: 1.0, PHASE_KEY: "downloading"}


def test_nothing_inside_the_minimum_gap(throttle, clock):
    state = {}
    throttle.due(state, "a", 0.0)
    clock.advance(3.9)
    assert not throttle.due(state, "a", 50.0)


@pytest.mark.parametrize("delta, expected", [(4.9, False), (5.0, True), (12.0, True), (-5.0, True)])
def test_milestone_delta_of_five_percent(throttle, clock, delta, expected):
    state = {}
    throttle.due(state, "a", 50.0)
    clock.advance(5)
    assert throttle.due(state, "a", 50.0 + delta) is expected


def test_small_steps_accumulate_until_a_milestone(throttle, clock):
    state = {}
    throttle.due(state, "a", 10.0)
    edits = []
    for pct in (11, 12, 13, 14, 15, 16):
        clock.advance(2)  # 12 s in total: never reaches the 15 s heartbeat
        if throttle.due(state, "a", float(pct)):
            edits.append(pct)
    # 15 is the first value 5 % past the last edit (10); 16 comes 2 s later,
    # inside the gap.
    assert edits == [15]
    assert state[PCT_KEY] == 15.0


def test_milestone_is_measured_from_last_edit_not_last_call(throttle, clock):
    state = {}
    throttle.due(state, "a", 0.0)
    for pct in (2.0, 4.0):
        clock.advance(4)
        assert not throttle.due(state, "a", pct)
    clock.advance(4)
    assert throttle.due(state, "a", 6.0)
    assert state[PCT_KEY] == 6.0


def test_unknown_progress_waits_for_heartbeat(throttle, clock):
    state = {}
    throttle.due(state, "a", None)
    clock.advance(14)
    assert not throttle.due(state, "a", None)
    clock.advance(1)
    assert throttle.due(state, "a", None)


def test_known_progress_after_unknown_passes(throttle, clock):
    state = {}
    throttle.due(state, "a", None)
    clock.advance(4)
    assert throttle.due(state, "a", 1.0)


def test_heartbeat_edits_even_without_progress(throttle, clock):
    state = {}
    throttle.due(state, "a", 30.0)
    clock.advance(15)
    assert throttle.due(state, "a", 30.0)


def test_phase_change_counts_as_milestone(throttle, clock):
    state = {}
    throttle.due(state, "a", 30.0, phase="downloading")
    clock.advance(4)
    assert throttle.due(state, "a", 30.0, phase="paused")
    clock.advance(1)
    assert not throttle.due(state, "a", 30.0, phase="downloading")  # still within the gap


@pytest.mark.parametrize("flag", ["force", "final"])
def test_force_and_final_bypass_everything(throttle, clock, flag):
    state = {}
    throttle.due(state, "a", 30.0)
    assert throttle.due(state, "a", 30.0, **{flag: True})
    assert state[AT_KEY] == clock.now


def test_mark_records_an_edit_made_elsewhere(throttle, clock):
    state = {}
    throttle.mark(state, "a", 20.0, phase="downloading")
    assert throttle.active_count() == 1
    clock.advance(4)
    assert not throttle.due(state, "a", 22.0, phase="downloading")
    assert throttle.due(state, "a", 25.0, phase="downloading")


def test_state_with_string_numbers(throttle, clock):
    state = {AT_KEY: str(clock.now), PCT_KEY: "10"}
    clock.advance(5)
    assert not throttle.due(state, "a", 12.0)
    assert throttle.due(state, "a", 15.0)


# ------------------------------------------------------------ concurrency


def _simulate(throttle, clock, tasks, seconds, step=0.5, rate=1.0):
    """``tasks`` messages each gaining ``rate`` % per second; returns edits per task."""
    states = {key: {} for key in range(tasks)}
    edits = {key: 0 for key in range(tasks)}
    elapsed = 0.0
    while elapsed < seconds:
        for key, state in states.items():
            if throttle.due(state, key, min(100.0, elapsed * rate)):
                edits[key] += 1
        clock.advance(step)
        elapsed += step
    return edits


def test_single_task_edits_roughly_every_milestone(throttle, clock):
    edits = _simulate(throttle, clock, tasks=1, seconds=100)
    # 1 %/s: a 5 % milestone every 5 s, plus the first send.
    assert 18 <= edits[0] <= 21


def test_concurrent_tasks_share_one_budget(throttle, clock):
    edits = _simulate(throttle, clock, tasks=12, seconds=120)
    # 12 tasks: each gap is 18 s, so all together stay near one edit per 1.5 s.
    total = sum(edits.values())
    assert total <= 120 / 1.5 + 12
    # and every task still refreshes at least once per heartbeat.
    assert min(edits.values()) >= 120 // 18
    # No message is starved compared to another.
    assert max(edits.values()) - min(edits.values()) <= 1


def test_more_tasks_mean_fewer_edits_each(clock):
    def per_task(n):
        throttle = AdaptiveThrottle(clock=clock)
        return max(_simulate(throttle, clock, tasks=n, seconds=120, rate=5.0).values())

    assert per_task(1) > per_task(6) > per_task(16)


def test_finished_tasks_stop_counting(throttle, clock):
    for key in range(10):
        throttle.touch(key)
    assert throttle.interval() == 15.0
    for key in range(9):
        throttle.forget(key)
    assert throttle.interval() == 4.0


def test_idle_tasks_age_out_of_the_budget(throttle, clock):
    for key in range(10):
        throttle.touch(key)
    clock.advance(61)
    state = {}
    throttle.due(state, "live", 0.0)
    clock.advance(4)
    assert throttle.due(state, "live", 5.0)  # only one active: 4 s gap


# ------------------------------------------------------------ bot wiring


def test_bot_throttle_uses_configured_milestone_and_heartbeat():
    assert tb.edit_throttle.milestone == tb.STATUS_MILESTONE_PERCENT
    assert tb.edit_throttle.max_interval == tb.STATUS_HEARTBEAT_SECONDS
    assert tb.edit_throttle.min_interval == tb.CARD_MIN_INTERVAL


async def test_job_card_refresh_follows_the_throttle(monkeypatch, clock):
    throttle = AdaptiveThrottle(clock=clock)
    monkeypatch.setattr(tb, "edit_throttle", throttle)
    edits = []

    async def edit(app, chat_id, message_id, text, markup, parse_mode=None):
        edits.append(text)
        return True

    monkeypatch.setattr(tb, "_edit_dashboard_message", edit)
    job = {
        "id": 3, "name": "f", "status": "downloading", "chat_id": 1, "user_id": 1,
        "total_length": 100, "completed_length": 10, "started_at": 0,
        "card": {"chat_id": 1, "message_id": 5, "hash": None},
    }
    throttle.mark(job["card"], job_cards.card_throttle_key(job), job_cards.card_progress(job), phase="downloading")

    clock.advance(5)
    job["completed_length"] = 12
    await tb.refresh_job_card(None, job)
    assert edits == []  # 2 %: below the milestone
    job["completed_length"] = 16
    await tb.refresh_job_card(None, job)
    assert len(edits) == 1  # 6 % since the last edit
    job["completed_length"] = 30
    await tb.refresh_job_card(None, job)
    assert len(edits) == 1  # inside the gap
    await tb.refresh_job_card(None, job, force=True)
    assert len(edits) == 2
