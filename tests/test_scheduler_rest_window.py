"""The night-rest window: a human account goes quiet overnight.

Running the highest-volume loop straight through the night is the strongest
machine-vs-human signal. These pin the wrap-aware window test and the
client-level pause API.

The second paragraph of this docstring used to say the farm loop "consults" that
API to sleep until morning. It did not, and nor did any other loop: until
`can_continue` learned about the window, `is_rest_window`, `rest_pause_seconds`
and `seconds_until_rest_ends` had no callers between them anywhere in the
project. The machinery was written, tested and unreachable.
"""

from datetime import datetime

from travian_api.stealth.scheduler import ActivityScheduler


def _at(hour: float) -> datetime:
    h = int(hour)
    m = int((hour - h) * 60)
    return datetime(2026, 1, 1, h, m)


class TestRestWindowWrapAware:
    def _sched(self, start: float, end: float) -> ActivityScheduler:
        s = ActivityScheduler(enabled=True)
        s._night_start_hour = start
        s._night_end_hour = end
        return s

    def test_window_wrapping_past_midnight(self):
        s = self._sched(23.0, 6.0)
        assert s.is_rest_window(_at(23.5)) is True
        assert s.is_rest_window(_at(2.0)) is True
        assert s.is_rest_window(_at(5.9)) is True
        assert s.is_rest_window(_at(6.1)) is False
        assert s.is_rest_window(_at(14.0)) is False
        assert s.is_rest_window(_at(22.5)) is False

    def test_non_wrapping_window(self):
        s = self._sched(1.0, 7.0)
        assert s.is_rest_window(_at(3.0)) is True
        assert s.is_rest_window(_at(0.5)) is False
        assert s.is_rest_window(_at(8.0)) is False

    def test_midnight_boundary_start(self):
        # seed_circadian can draw start up to 24.0; it must normalize to 0.
        s = self._sched(24.0, 6.0)
        assert s.is_rest_window(_at(0.5)) is True
        assert s.is_rest_window(_at(5.9)) is True
        assert s.is_rest_window(_at(7.0)) is False

    def test_disabled_scheduler_never_rests(self):
        s = ActivityScheduler(enabled=False)
        s._night_start_hour = 23.0
        s._night_end_hour = 6.0
        assert s.is_rest_window(_at(2.0)) is False


class TestSecondsUntilRestEnds:
    """One pause must span the whole remaining window — never oversleep past
    morning, never undersleep and wake back inside it."""

    def _sched(self, start=23.0, end=6.0) -> ActivityScheduler:
        s = ActivityScheduler(enabled=True)
        s._night_start_hour = start
        s._night_end_hour = end
        return s

    def test_zero_outside_the_window(self):
        assert self._sched().seconds_until_rest_ends(_at(14.0)) == 0.0

    def test_spans_to_window_end_from_early_night(self):
        # 23:00 with end 06:00 → ~7h (+ up to 45min buffer), waking near morning.
        secs = self._sched().seconds_until_rest_ends(_at(23.0))
        assert 7 * 3600 <= secs <= 7 * 3600 + 2700

    def test_short_when_entering_near_the_end(self):
        # 05:30 with end 06:00 → ~30min, NOT a full 6-9h night-break draw.
        secs = self._sched().seconds_until_rest_ends(_at(5.5))
        assert 0.5 * 3600 <= secs <= 0.5 * 3600 + 2700

    def test_the_exact_window_edge_never_returns_a_full_day(self):
        # One second before the end (05:59:59, end 06:00): the remaining-time
        # math and the window check share one instant, so it must be ~1s (+
        # buffer), never the ~24h wrap the old double-clock-sample could produce.
        s = self._sched()
        secs = s.seconds_until_rest_ends(datetime(2026, 1, 1, 5, 59, 59))
        assert 0 <= secs <= 2700 + 5, secs

    def test_one_pause_clears_the_window_so_wake_is_past_it(self):
        s = self._sched()
        for enter in (23.0, 0.5, 2.0, 5.9):
            secs = s.seconds_until_rest_ends(_at(enter))
            # The base (minus buffer) lands exactly at the window end (06:00),
            # so waking is at/after morning — never back inside the window.
            base_h = (secs - 0.0) / 3600.0
            hour = enter
            woke = (hour + base_h) % 24.0
            # Allow the 0-45min buffer to push slightly past 06:00.
            assert 6.0 <= woke <= 6.75 + 1e-6, (enter, woke)


class TestClientRestPause:
    def _client(self):
        from travian_api.clients.http_client import HttpClient
        from travian_api.config import Settings

        return HttpClient(
            Settings(
                base_url="https://ts2.x1.europe.travian.com",
                username="u@example.com",
                password="pw",
            )
        )

    def test_pause_is_zero_when_not_in_window(self, monkeypatch):
        client = self._client()
        monkeypatch.setattr(client._activity_scheduler, "seconds_until_rest_ends", lambda: 0.0)
        assert client.rest_pause_seconds() == 0.0

    def test_pause_is_the_window_aligned_duration(self, monkeypatch):
        client = self._client()
        monkeypatch.setattr(
            client._activity_scheduler, "seconds_until_rest_ends", lambda: 5 * 3600.0
        )
        assert client.rest_pause_seconds() == 5 * 3600.0

    def test_pause_is_zero_when_stealth_off(self, monkeypatch):
        client = self._client()
        client._stealth_enabled = False
        # Even if it "would" be the rest window, stealth-off returns 0.
        monkeypatch.setattr(
            client._activity_scheduler, "seconds_until_rest_ends", lambda: 5 * 3600.0
        )
        assert client.rest_pause_seconds() == 0.0


class TestTheWindowActuallyStopsTheLoops:
    """`can_continue()` is the one place all three loops pass through.

    It answered on quotas alone -- have I worked too long today, too long in
    one sitting -- and said nothing about what time it is. So the night branch
    of `next_break_duration`, which was correct, could only be reached if the
    daily budget happened to run out inside the window.
    """

    def _sched(self, start: float, end: float) -> ActivityScheduler:
        s = ActivityScheduler(enabled=True)
        s._night_start_hour = start
        s._night_end_hour = end
        return s

    def test_a_fresh_scheduler_still_works_outside_the_window(self):
        """The premise: nothing here stops an ordinary daytime loop."""
        s = self._sched(23.0, 6.0)
        s._night_start_hour, s._night_end_hour = 23.0, 6.0
        # Not asserting on wall-clock time: force the answer both ways below.
        assert s.is_rest_window(_at(14.0)) is False

    def test_can_continue_is_false_during_the_window(self, monkeypatch):
        s = self._sched(23.0, 6.0)
        monkeypatch.setattr(s, "is_rest_window", lambda *a, **k: True)

        assert s.can_continue() is False

    def test_can_continue_is_true_outside_it(self, monkeypatch):
        s = self._sched(23.0, 6.0)
        monkeypatch.setattr(s, "is_rest_window", lambda *a, **k: False)

        assert s.can_continue() is True

    def test_a_disabled_scheduler_never_rests(self):
        """Stealth off means stealth off -- the window must not leak through."""
        s = ActivityScheduler(enabled=False)

        assert s.is_rest_window(_at(3.0)) is False
        assert s.can_continue() is True


class TestTheNightPauseCoversTheWholeWindow:
    """A break that lands short is worse than no break at all.

    It puts activity at 4am AND a gap that looks deliberate. A break that lands
    long wastes the morning. The old branch drew 6-9h from wherever the loop
    happened to be standing in the window, so it could do either; it now
    delegates to `seconds_until_rest_ends`, which measures from the window's END.

    Compared against the window end computed here rather than against a second
    call, because that method carries its own wake-time buffer -- two samples of
    one decision is exactly what this change removed.
    """

    WINDOW = (23.0, 6.0)
    BUFFER_MAX_S = 2700.0  # the buffer seconds_until_rest_ends adds

    def _sched(self) -> ActivityScheduler:
        s = ActivityScheduler(enabled=True)
        s._night_start_hour, s._night_end_hour = self.WINDOW
        return s

    def _floor_seconds(self, now) -> float:
        """Time from *now* to the window's end, with no buffer."""
        hour = now.hour + now.minute / 60.0 + now.second / 3600.0
        return ((self.WINDOW[1] - hour) % 24.0) * 3600.0

    def test_it_never_wakes_back_inside_the_window(self):
        s = self._sched()
        for hour in (23.1, 0.5, 2.0, 4.0, 5.9):
            now = _at(hour)
            floor = self._floor_seconds(now)
            for _ in range(40):
                assert s.next_break_duration(now) >= floor

    def test_it_never_oversleeps_far_past_morning(self):
        """The other half: a 6-9h draw entered at 05:30 slept through the day."""
        s = self._sched()
        now = _at(5.5)

        for _ in range(60):
            overshoot = s.next_break_duration(now) - self._floor_seconds(now)
            assert 0.0 <= overshoot <= self.BUFFER_MAX_S

    def test_the_wake_time_is_not_a_daily_constant(self):
        s = self._sched()
        now = _at(1.0)
        draws = {round(s.next_break_duration(now)) for _ in range(200)}

        assert len(draws) > 1, "waking at the identical second every morning is its own tell"

    def test_outside_the_window_it_is_an_ordinary_short_break(self):
        s = self._sched()

        assert s.next_break_duration(_at(14.0)) < 3600.0


class TestTheBreakIsMeasuredOnTheSameClockThatRefusedTheWork:
    """`next_break_duration()` resolved its own default `now` from the HOST.

    Every other method here moved to `_server_now()` when the scheduler learned
    what time it is where the GAME is. This one kept `datetime.now()` -- and
    because it then passes that value INTO `is_rest_window(now)`, it did not
    merely disagree with that method, it overrode it.

    With the host three hours ahead of a server at 04:00 and a 23:00-06:00
    window, `can_continue()` read the server clock, found the night and refused
    to work, while this read 07:00, found no window, and returned a ten-minute
    daytime break with two hours of night still to run. `BuildQueueService`
    sleeps whatever it is handed and resumes, so the account woke up inside its
    own night and carried on.

    Its own docstring already required all three to agree about where the
    window is. This is the line that did not.
    """

    WINDOW = (23.0, 6.0)

    def _sched(self, *, server_offset_minutes: int) -> ActivityScheduler:
        s = ActivityScheduler(enabled=True)
        s._night_start_hour, s._night_end_hour = self.WINDOW
        s.set_server_utc_offset_minutes(server_offset_minutes)
        return s

    @staticmethod
    def _pin_server_clock(s: ActivityScheduler, hour: float) -> None:
        """Pin `_server_now`, leaving the host clock wherever it really is.

        The point of the test is that the two differ, so the host side is
        deliberately left as the real `datetime.now()` -- whatever the machine
        running the suite happens to say. The bug reproduces for every host time
        outside the window, which is most of the day.
        """
        s._server_now = lambda: _at(hour)

    def test_a_night_on_the_server_gets_a_night_length_break(self):
        s = self._sched(server_offset_minutes=60)
        self._pin_server_clock(s, 4.0)

        # 04:00 server, window ends 06:00 -> about two hours, plus the wake
        # buffer. A short break here is the bug: ten to twenty minutes.
        duration = s.next_break_duration()

        assert duration >= 2.0 * 3600.0, (
            f"got {duration / 60:.0f}min with two hours of night left -- "
            f"the break was measured on the host clock"
        )

    def test_it_agrees_with_the_method_that_refuses_the_work(self):
        """`can_continue` and `next_break_duration` must not split: one saying
        stop and the other handing back a daytime break is how a loop resumes
        mid-window."""
        s = self._sched(server_offset_minutes=60)
        self._pin_server_clock(s, 4.0)

        assert s.can_continue() is False
        assert s.next_break_duration() >= 3600.0

    def test_an_explicit_now_is_still_honoured(self):
        """The parameter exists so all three can be driven from one instant."""
        s = self._sched(server_offset_minutes=60)
        self._pin_server_clock(s, 4.0)

        assert s.next_break_duration(_at(14.0)) < 3600.0

    def test_with_no_offset_known_it_is_the_host_clock_exactly_as_before(self):
        """`_server_now` falls back to `datetime.now()` when no page has stated
        the offset, so this change is inert until one has."""
        s = self._sched(server_offset_minutes=None)

        assert s._server_now is not None
        assert s.next_break_duration(_at(14.0)) < 3600.0
