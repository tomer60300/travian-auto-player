"""The night-rest window: a human account goes quiet overnight.

Running the highest-volume loop straight through the night is the strongest
machine-vs-human signal. These pin the wrap-aware window test and the
client-level pause API that the farm loop consults to sleep until morning.
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
