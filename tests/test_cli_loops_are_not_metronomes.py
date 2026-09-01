"""The CLI's recurring loops must not tick on a fixed interval.

The web-driven loops learned this twice already: `_loop_stealth.recurring_wait`
exists because "a fixed inter-cycle interval is a razor-sharp periodogram peak",
and `HttpClient.tempo_scale` exists so a session's short gaps and its
super-cadence drift together rather than one staying independent of the other.

The CLI's `farm run`, `farm run-all` and `scout auto --interval` never got
either. They slept the raw `--interval` between rounds, so a run produced
requests at exactly t, t+300, t+600, … for as long as it was left going --
a delta spike at 1/interval Hz on any periodogram of the account's request
times, which is the single most textbook automation signature there is. These
commands are installed under the `travian` console script and are reachable by
name, so the exposure was real rather than theoretical.

What is asserted here is a DISTRIBUTION, not a constant: successive waits must
differ, they must not be a tidy multiple of the interval, and their average has
to stay near the interval the operator asked for -- a stealth fix that silently
halves the configured cadence would be its own kind of wrong.
"""

import statistics

import pytest

from travian_api.stealth.timing import HumanTiming


class _FakeClient:
    """Enough HttpClient surface for the wait helper, with stealth on."""

    stealth_enabled = True

    def __init__(self):
        self.tempo_calls = 0

    def tempo_scale(self, value: float) -> float:
        # The real one couples the wait to the session's AR(1) tempo. Identity
        # here so the test measures the DRAW's shape, not the tempo walk.
        self.tempo_calls += 1
        return value


class _StealthOffClient:
    stealth_enabled = False

    def tempo_scale(self, value: float) -> float:  # pragma: no cover - never called
        raise AssertionError("tempo_scale must not be consulted when stealth is off")


def _waits(client, interval, n=200):
    from travian_api.cli import _loop_wait

    return [_loop_wait(client, interval) for _ in range(n)]


class TestTheIntervalIsNoLongerFixed:
    @pytest.mark.parametrize("interval", [60, 300, 900])
    def test_successive_waits_differ(self, interval):
        client = _FakeClient()

        waits = _waits(client, interval)

        # Measures VARIETY, not uniqueness: the sampler rounds to a tenth of a
        # second, so a few exact repeats across hundreds of draws spanning
        # hundreds of seconds are quantisation, not cadence. The first version
        # of this test demanded >90% distinct values and was really asserting
        # the rounding granularity.
        assert len(set(waits)) > len(waits) * 0.5, (
            f"waits repeat heavily: {len(set(waits))} distinct out of {len(waits)} -- "
            f"a periodogram would still find the cadence"
        )
        assert statistics.pstdev(waits) > interval * 0.2, (
            f"standard deviation is only {statistics.pstdev(waits) / interval:.0%} of "
            f"the interval; the cadence is still effectively fixed"
        )

    def test_no_wait_is_exactly_the_interval(self):
        client = _FakeClient()

        waits = _waits(client, 300)

        assert 300.0 not in waits, "a wait landed exactly on the configured interval"

    def test_the_spread_is_wide_enough_to_smear_the_peak(self):
        """A jitter of a few percent is not a fix -- it moves the spike, it does
        not remove it. The heavy tail is the point."""
        client = _FakeClient()

        waits = _waits(client, 300)
        spread = (max(waits) - min(waits)) / 300.0

        assert spread > 0.5, (
            f"total spread is only {spread:.0%} of the interval; a narrow band is "
            f"still a detectable cadence"
        )

    def test_the_tempo_is_consulted(self):
        """Coupling to the session rhythm is half the reason this helper exists:
        a cadence that drifts independently of the short gaps is its own tell."""
        client = _FakeClient()

        _waits(client, 300, n=5)

        assert client.tempo_calls == 5


class TestItStillObeysTheOperator:
    @pytest.mark.parametrize("interval", [60, 300, 900])
    def test_the_average_cadence_stays_near_what_was_asked_for(self, interval):
        """Stealth must not quietly change the rate. Slower is acceptable
        (fewer requests than asked); materially faster would be the fix causing
        the harm it was meant to prevent."""
        client = _FakeClient()

        mean = statistics.fmean(_waits(client, interval, n=400))

        # The 4x tail cut removes right-hand mass and so pulls the mean DOWN;
        # uncompensated that is ~19% MORE traffic than configured, which is why
        # _TAIL_CUT_COMPENSATION exists. This asserts the OUTCOME, so a change to
        # the sampler's shape fails here rather than silently speeding up every
        # loop the operator leaves running.
        assert mean >= interval * 0.9, (
            f"mean wait {mean:.0f}s is well under the {interval}s asked for -- this "
            f"sends MORE traffic than the operator configured"
        )
        assert mean <= interval * 2.5, (
            f"mean wait {mean:.0f}s more than doubles the {interval}s cadence; the "
            f"loop would do far less than the operator expects"
        )

    def test_no_single_wait_is_long_enough_to_look_like_a_hang(self):
        client = _FakeClient()

        waits = _waits(client, 300, n=400)

        assert max(waits) <= 300 * 5, (
            f"longest wait {max(waits):.0f}s is >5x the interval; an operator would "
            f"reasonably think the loop had stuck"
        )

    def test_every_wait_is_positive(self):
        client = _FakeClient()

        assert min(_waits(client, 60, n=400)) > 0


class TestStealthOffStaysDeterministic:
    def test_the_raw_interval_is_used_when_stealth_is_disabled(self):
        """Dev and test runs need predictable timing, and the rest of the
        codebase makes the same exemption."""
        from travian_api.cli import _loop_wait

        assert _loop_wait(_StealthOffClient(), 300) == 300.0

    def test_a_client_without_the_stealth_attribute_is_treated_as_off(self):
        """Fail safe toward predictability, not toward an exception: a bare
        object must not crash a loop the operator is depending on."""
        from travian_api.cli import _loop_wait

        assert _loop_wait(object(), 120) == 120.0


class TestTheHelperUsesTheProjectsOwnSampler:
    def test_it_draws_through_human_timing(self, monkeypatch):
        """Not a bespoke jitter formula: the same heavy-tailed sampler every
        other gap in this codebase uses, so there is one distribution to reason
        about rather than two."""
        from travian_api import cli

        seen = []

        def _spy(mean, **kw):
            seen.append((mean, kw))
            return mean

        monkeypatch.setattr(cli.HumanTiming, "delay", staticmethod(_spy))
        cli._loop_wait(_FakeClient(), 300)

        assert seen, "the wait did not go through HumanTiming.delay"
        assert seen[0][0] == pytest.approx(300, rel=0.5)


def test_no_cli_loop_sleeps_on_a_bare_interval():
    """The regression guard. A new recurring command that sleeps the raw
    interval reintroduces the periodogram spike, and a reviewer will not catch
    it by eye in a 2,600-line CLI."""
    from pathlib import Path

    src = Path(cli_path := __import__("travian_api.cli", fromlist=["__file__"]).__file__)
    text = open(src, encoding="utf-8").read()
    assert cli_path

    offenders = [
        line.strip()
        for line in text.splitlines()
        if "asyncio.sleep(interval)" in line and not line.strip().startswith("#")
    ]
    assert not offenders, (
        f"a CLI loop sleeps the raw interval: {offenders} -- route it through "
        f"_loop_wait so the cadence is not a fixed tick"
    )


def test_the_sampler_itself_is_heavy_tailed():
    """Sanity on the dependency, so a change to HumanTiming that flattened the
    distribution would fail here rather than silently weaken every loop."""
    draws = [HumanTiming.delay(300, variance_factor=1.0) for _ in range(500)]

    assert statistics.median(draws) < statistics.fmean(draws), (
        "the sampler is not right-skewed; a symmetric jitter leaves a narrower, "
        "more detectable band than a heavy tail"
    )
