import ScrollableTable from './ScrollableTable'
import {
  attendanceFor,
  suggestedAttendance,
  unansweredAttendance,
} from '../utils/plannerNpc'
import {
  coversSmallHours,
  describeOvernight,
  dispatchWindowFor,
  windowDayShare,
  MINUTES_IN_DAY,
} from '../utils/plannerClock'

/** The day, as one picture: every profile's hours side by side, and who is
 *  awake during each of them.
 *
 * This panel exists because ONE question had nowhere to be asked. Section 7's
 * NPC balancing needs `npc_attended` per profile, the backend refuses a plan
 * without it as soon as a village keeps a stock floor, and the page had no
 * control at all -- so setting a floor and using day/night profiles simply
 * failed with a validation error naming village ids.
 *
 * It could have been one checkbox in the profile bar. It is a table of every
 * profile instead, and that is the design decision worth stating: the answer is
 * ASYMMETRIC and the asymmetry is the whole content. This operator is awake for
 * the day window and asleep through the night one, so the two profiles must
 * carry opposite answers -- and a single checkbox on whichever profile happens
 * to be selected shows one half of a pair whose interest is entirely in the
 * contrast. Seen together, "Day: you are at the marketplace" above "Night:
 * nobody is trading" is self-evidently right or self-evidently wrong. Seen one
 * at a time it is something to remember.
 *
 * So: one row per profile, its hours as an arc of the same 24-hour track, its
 * share of the day, and its answer. The clock may SUGGEST -- a window holding
 * the small hours is offered "asleep" as a chip -- and may never decide,
 * because "the operator is asleep" is a fact about the operator.
 *
 * Section 6's `overnight` is the second answer about these same hours, and it
 * is asked in the same table for the same reason: which profile is the one the
 * operator sleeps through decides where the closing deadline and the morning
 * floor land, and that is a statement about the day as a whole rather than
 * about whichever profile happens to be selected. Here the clock is allowed to
 * DERIVE rather than only suggest -- a window that wraps past midnight is the
 * night, which is right for a night stated as one 23:00-07:00 pair -- and the
 * declaration overrules it. Seen side by side, a split night's two halves
 * reading "from the hours: this is the night" above "from the hours: not the
 * night" is self-evidently wrong, which is the same argument the attendance
 * column is built on.
 *
 * Data in, callbacks out, and no effects: it renders from props alone so
 * `renderToString` reaches all of it, unlike the stage it sits in.
 */
export default function DayNightPanel({
  profileNames,
  activeProfile,
  profileWindows,
  profileAttendance,
  profileOvernight,
  attendanceRequired,
  reservedWindow,
  onWindow,
  onAttendance,
  onOvernight,
  onReservedWindow,
  onSelectProfile,
}) {
  const withHours = profileNames.filter((name) => dispatchWindowFor(profileWindows[name]) != null)
  const unanswered = attendanceRequired ? unansweredAttendance(withHours, profileAttendance) : []
  // Profiles with NO hours and no answer. Not the refusal above -- the backend
  // accepts a windowless request without the field -- and worse for being
  // accepted: a missing answer there is read as UNATTENDED, so the allowance is
  // zero and the plan quietly under-delivers instead of stopping. A set with no
  // window is not a set with no night; it owns all 24 hours, the eight nobody
  // is at the Marketplace included, and Travian cannot confine a repeat
  // interval to part of the day.
  const roundTheClockSilent = attendanceRequired
    ? profileNames.filter(
        (name) => !withHours.includes(name) && attendanceFor(profileAttendance, name) === null
      )
    : []
  // Every profile awake, and one of them running through the small hours. Not
  // an error -- a night shift is a real thing -- but it is the shape of a
  // forgotten answer, and the consequence is a night route funded by trading
  // nobody did.
  const claimsAwakeAtNight = withHours.filter(
    (name) =>
      attendanceFor(profileAttendance, name) === true && coversSmallHours(profileWindows[name])
  )
  // A night SPLIT at midnight, detected off the clock alone and with nothing
  // guessed: one profile's window ENDS at 00:00 and another's STARTS there.
  // The pre-midnight half wraps, so it derives as the night correctly; the
  // post-midnight half wraps in neither direction and derives as a day
  // profile. Undeclared, that is a 600-minute round trip inside a 420-minute
  // night going unreported as NIGHT_OVERRUN, and the 60% morning floor
  // measured at 00:00 instead of 07:00. Two profiles CAN both be the night --
  // that is what a split night is -- so this is not a contradiction to
  // refuse, it is the one derivation that needs a declaration over it.
  const endsAtMidnight = withHours.some((name) => dispatchWindowFor(profileWindows[name])[1] === 0)
  const splitNightUndeclared = endsAtMidnight
    ? withHours.filter(
        (name) =>
          dispatchWindowFor(profileWindows[name])[0] === 0 &&
          typeof profileOvernight?.[name] !== 'boolean'
      )
    : []

  return (
    <div className="card p-4">
      <div className="flex items-start justify-between gap-4 flex-wrap mb-3">
        <div className="min-w-[18rem] flex-1">
          <h3 className="font-semibold">The day, window by window</h3>
          <p className="text-secondary text-xs mt-1">
            Each profile is planned on its own, but the account lives through all of them.
            These are the hours each one owns — and, for section 7&apos;s NPC balancing,
            whether you are at the marketplace during them.
          </p>
        </div>
      </div>

      {/* The gate, stated before the table rather than after a failed build:
          the backend refuses the plan over exactly this, naming village ids. */}
      {unanswered.length > 0 && (
        <p className="text-warning text-xs mb-3" role="status">
          {'⚠ '}
          {unanswered.join(', ')} {unanswered.length === 1 ? 'has' : 'have'} hours and no
          answer for <strong>who is trading</strong>. A village here keeps an NPC-backed
          stock floor, so whether you are awake to do the trading decides whether those
          routes are funded at all — the plan is refused until every profile says.
        </p>
      )}

      {roundTheClockSilent.length > 0 && (
        <p className="text-warning text-xs mb-3" role="status">
          {'⚠ '}
          {roundTheClockSilent.join(', ')} {roundTheClockSilent.length === 1 ? 'has' : 'have'} no
          hours, so {roundTheClockSilent.length === 1 ? 'it runs' : 'they run'} round the clock —
          all 24 hours, including the ones nobody is at the marketplace. The plan is not refused
          over it, and that is the problem: an unanswered window counts as{' '}
          <strong>nobody trading</strong>, so the NPC allowance is zero and the routes those
          villages were meant to fund come back short.
        </p>
      )}

      {claimsAwakeAtNight.length > 0 && (
        <p className="text-warning text-xs mb-3">
          {claimsAwakeAtNight.join(', ')} runs through the small hours and says you are
          trading. If you are asleep then, say so — the allowance would otherwise fund
          cargo out of conversions nobody performs.
        </p>
      )}

      {splitNightUndeclared.length > 0 && (
        <p className="text-warning text-xs mb-3" role="status">
          {'⚠ '}
          Your night looks split at midnight, and {splitNightUndeclared.join(', ')}{' '}
          {splitNightUndeclared.length === 1 ? 'runs' : 'run'} the half after it. A window that
          does not wrap past midnight reads as a <strong>day</strong> profile, so section
          6&apos;s rules would not govern it: no closing deadline for its merchants, and the
          morning floor measured at 00:00 instead of the end of the night. Say so in the
          overnight column.
        </p>
      )}

      <ScrollableTable label="The day, window by window">
        <table className="w-full text-xs">
          <thead className="text-secondary uppercase">
            <tr>
              <th className="text-left py-1 px-2 sticky-col">Profile</th>
              <th className="text-left px-2">Runs</th>
              {/* The axis, once, at the top of the column every row's track
                  belongs to. Four labels at the same four fractions the ticks
                  use, so a slab's position is readable rather than guessed. */}
              <th className="text-left px-2 min-w-40">
                Hours of the day
                <span aria-hidden="true" className="day-axis">
                  {DAY_TICKS.map((hour) => (
                    <span
                      key={hour}
                      className={hour === 0 ? undefined : 'day-axis-mid'}
                      style={{ left: `${(hour / 24) * 100}%` }}
                    >
                      {String(hour).padStart(2, '0')}
                    </span>
                  ))}
                </span>
              </th>
              <th
                className="text-left px-2"
                title="Whether you are at the marketplace during these hours, so section 7's NPC conversion can actually be performed. Required as soon as any village keeps a stock floor: false means the conversion allowance is zero for this profile — the crop keeps growing, but nobody is converting it."
              >
                Who is trading
              </th>
              <th
                className="text-left px-2"
                title="Whether this profile is the one you sleep through, so section 6's rules govern it: no latency target, and every merchant home before the window closes. Left on 'from the hours' it is derived from the window wrapping past midnight — right for a night stated as one 23:00-07:00 pair, wrong for the half of a night split at midnight and for a day profile covering almost the whole day."
              >
                Overnight
              </th>
            </tr>
          </thead>
          <tbody>
            {profileNames.map((name) => {
              const hours = profileWindows[name] ?? null
              const pair = dispatchWindowFor(hours)
              const share = windowDayShare(hours)
              const answer = attendanceFor(profileAttendance, name)
              const declaredNight =
                typeof profileOvernight?.[name] === 'boolean' ? profileOvernight[name] : null
              const suggestion = suggestedAttendance(hours)
              const owed = attendanceRequired && pair != null && answer === null
              // The same question, unanswered, on a profile with no hours. It
              // does not refuse the plan -- the backend takes the request -- so
              // it is stated rather than enforced, but it is asked in the same
              // place and answered by the same chip.
              const silentAllDay = attendanceRequired && pair == null && answer === null
              const problemId = `attendance-problem-${name}`
              return (
                <tr
                  key={name}
                  className="row-focus touch-target border-t-default transition-colors"
                  /* Tokens, not `bg-violet-400/10`: a fixed Tailwind shade does
                     not flip with `[data-theme]`, and the word "editing" below
                     carries the state anyway. */
                  style={
                    name === activeProfile
                      ? {
                          backgroundColor:
                            'color-mix(in srgb, var(--md-primary) 10%, transparent)',
                        }
                      : undefined
                  }
                >
                  <td className="py-1.5 px-2 sticky-col row-focus-edge whitespace-nowrap">
                    {/* The active profile is named as such rather than only
                        tinted: the row highlight is the same colour the
                        Allocate grid uses for a checked row, and colour alone
                        is not a state. */}
                    <button
                      type="button"
                      className={`underline pointer-coarse:min-h-11 ${
                        name === activeProfile ? 'text-primary' : 'text-secondary hover:text-primary'
                      }`}
                      onClick={() => onSelectProfile(name)}
                    >
                      {name}
                    </button>
                    {name === activeProfile && (
                      <span className="block text-info text-[10px] uppercase">editing</span>
                    )}
                  </td>
                  <td className="px-2 whitespace-nowrap">
                    <span className="inline-flex items-center gap-1">
                      <input
                        type="time"
                        aria-label={`${name} window start`}
                        className="input-field text-xs py-0.5 px-1 w-auto"
                        value={(hours ?? ['', ''])[0]}
                        onChange={(e) => onWindow(name, [e.target.value, (hours ?? ['', ''])[1]])}
                      />
                      <span className="text-secondary">–</span>
                      <input
                        type="time"
                        aria-label={`${name} window end`}
                        className="input-field text-xs py-0.5 px-1 w-auto"
                        value={(hours ?? ['', ''])[1]}
                        onChange={(e) => onWindow(name, [(hours ?? ['', ''])[0], e.target.value])}
                      />
                    </span>
                  </td>
                  <td className="px-2 min-w-40">
                    {pair == null ? (
                      <span className="text-secondary">no hours set — skipped by the day check</span>
                    ) : (
                      <>
                        {/* Decorative: the same fact is in the figure beside
                            it, so the track carries aria-hidden rather than a
                            role nothing can read usefully. */}
                        <span aria-hidden="true" className="day-track">
                          {/* Midnight, 06:00, noon, 18:00 -- the whole reason
                              this bar was unreadable. Night's 23:00-07:00 drew
                              as two slabs on a bare pill with no tick, no axis
                              and no label anywhere, so which end was which, and
                              whether the two were one band or two windows, was
                              unknowable. The axis under the column heading
                              carries the same four positions. */}
                          {DAY_TICKS.map((hour) => (
                            <span
                              key={hour}
                              className="day-track-tick"
                              style={{ left: `${(hour / 24) * 100}%` }}
                            />
                          ))}
                          {arcs(pair).map((arc, i) => (
                            <span
                              key={i}
                              className="day-track-band"
                              style={{
                                left: `${arc.left}%`,
                                width: `${arc.width}%`,
                                // A wrap is ONE band, drawn leaving the right
                                // edge and re-entering the left: the cap is
                                // rounded where the window really begins or
                                // ends and SQUARE where it runs off the track,
                                // so 23:00-07:00 reads as a single night rather
                                // than as two unrelated pills. `arcs` returns
                                // the late piece first, so index 0 of a pair is
                                // the one leaving the right edge.
                                borderRadius:
                                  arcs(pair).length === 1
                                    ? '9999px'
                                    : i === 0
                                      ? '9999px 0 0 9999px'
                                      : '0 9999px 9999px 0',
                                background:
                                  answer === false ? 'var(--md-outline)' : 'var(--md-primary)',
                              }}
                            />
                          ))}
                        </span>
                        <span className="text-secondary font-mono">
                          {Math.round(share * 24)}h · {Math.round(share * 100)}% of the day
                        </span>
                      </>
                    )}
                  </td>
                  <td className="px-2">
                    <select
                      aria-label={`Who is trading during ${name}`}
                      aria-invalid={owed || undefined}
                      aria-describedby={owed || silentAllDay ? problemId : undefined}
                      className="input-field w-auto text-xs py-1"
                      value={answer === null ? '' : answer ? 'awake' : 'asleep'}
                      onChange={(e) =>
                        onAttendance(
                          name,
                          e.target.value === '' ? null : e.target.value === 'awake'
                        )
                      }
                    >
                      <option value="">Not answered</option>
                      <option value="awake">You are at the marketplace</option>
                      <option value="asleep">Nobody is trading</option>
                    </select>
                    {/* No echo under the select. It printed
                        `describeAttendance(answer)` -- "you are at the
                        marketplace" -- in 11px grey directly under the option
                        that reads "You are at the marketplace", so the same
                        answer was on screen twice, one of the two illegible.
                        The words still exist where they are the only statement
                        of the answer: the profile bar's chip. */}
                    {(owed || silentAllDay) && (
                      <>
                        <span id={problemId} className="block text-warning mt-0.5">
                          {owed
                            ? 'the plan is refused until this says'
                            : 'no hours, so this runs round the clock — silence counts as nobody trading'}
                        </span>
                        {/* The clock's guess, offered rather than applied. One
                            press, and the label names the answer it will
                            write so it is never a mystery button. A profile
                            with no hours gets its own wording, because "these
                            hours" names hours it does not have -- and the
                            guess there is asleep, since round the clock covers
                            the small hours rather than skipping them. */}
                        <button
                          type="button"
                          className="block underline text-info hover:text-primary mt-0.5 pointer-coarse:min-h-11"
                          onClick={() => onAttendance(name, suggestion)}
                        >
                          {pair == null
                            ? 'Round the clock covers the small hours too — say you are asleep'
                            : `These hours look like ${
                                suggestion ? 'you are awake' : 'you are asleep'
                              } — say so`}
                        </button>
                      </>
                    )}
                  </td>
                  {/* Section 6's own answer, beside section 7's because both
                      are questions ABOUT these hours. Three states, and the
                      resting one is DERIVE rather than a boolean: the clock is
                      right for a night stated as one 23:00-07:00 window, and
                      overriding it is the exception. Disabled where there are
                      no hours, because the backend refuses a declaration with
                      no window to measure the deadline against -- and a
                      disabled field looks disabled. */}
                  <td className="px-2">
                    <select
                      aria-label={`Is ${name} the overnight profile`}
                      className="input-field w-auto text-xs py-1"
                      disabled={pair == null}
                      value={
                        typeof declaredNight === 'boolean'
                          ? declaredNight
                            ? 'night'
                            : 'day'
                          : ''
                      }
                      onChange={(e) =>
                        onOvernight(
                          name,
                          e.target.value === '' ? null : e.target.value === 'night'
                        )
                      }
                    >
                      <option value="">From the hours</option>
                      <option value="night">The night you sleep through</option>
                      <option value="day">Not the night</option>
                    </select>
                    <span className="block text-secondary mt-0.5">
                      {describeOvernight(declaredNight, hours)}
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </ScrollableTable>

      {/* The other half of section 7's attendance, and the half that is about
          the PERSON rather than the profile: when the operator actually sits
          down at the marketplace. Account-wide, because it is one person at
          one marketplace -- the answer above is per profile because they are
          awake for some windows and not others.

          A preference the planner WEIGHS, never a refusal: arrivals avoid it
          where an alternative exists and the plan says so when the geometry
          forces one in. So an unset window is silence, and the pair is dropped
          from the request rather than sent zero-width. */}
      <div className="border-t-default pt-3 mt-3">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-[16rem] flex-1">
            <p className="text-xs font-semibold">Keep arrivals clear of your NPC burst</p>
            <p className="text-secondary text-xs mt-0.5">
              The hours you actually sit at the marketplace. Deliveries are steered away from
              them where a route has an alternative, so a merchant arriving does not compete
              with the trading you are doing by hand — and the plan says so when the
              geometry leaves it no choice. Leave both blank to reserve nothing.
            </p>
          </div>
          <span className="flex items-center gap-1 text-xs text-secondary">
            <input
              type="time"
              aria-label="NPC burst window start"
              className="input-field text-xs py-0.5 px-1 w-auto"
              value={(reservedWindow ?? ['', ''])[0]}
              onChange={(e) => onReservedWindow(pairOrNull([e.target.value, (reservedWindow ?? ['', ''])[1]]))}
            />
            –
            <input
              type="time"
              aria-label="NPC burst window end"
              className="input-field text-xs py-0.5 px-1 w-auto"
              value={(reservedWindow ?? ['', ''])[1]}
              onChange={(e) => onReservedWindow(pairOrNull([(reservedWindow ?? ['', ''])[0], e.target.value]))}
            />
          </span>
        </div>
        <p className="text-secondary text-xs mt-1">
          {dispatchWindowFor(reservedWindow)
            ? `Arrivals avoid ${reservedWindow[0]}–${reservedWindow[1]} where they can.`
            : 'Nothing reserved — arrivals may land at any hour.'}
        </p>
      </div>

      {!attendanceRequired && (
        <p className="text-secondary text-xs mt-2">
          No village keeps an NPC-backed stock floor, so nothing here is required — set a{' '}
          <span className="text-primary">Stock floor %</span> on the Account stage and this
          becomes the question that decides whether those routes are funded.
        </p>
      )}
    </div>
  )
}

/** A half-typed time pair is not a window.
 *
 * Both boxes empty is null, which is how the request says "reserve nothing" --
 * and the state has to reach null rather than `['', '']`, or clearing one box
 * would leave a pair the payload has to remember to reject. One box filled is
 * kept, because the operator is mid-edit and blanking their first entry would
 * be hostile.
 */
function pairOrNull(pair) {
  return pair[0] || pair[1] ? pair : null
}

/** A window as one or two arcs of a 24-hour track, in percent.
 *
 * Two when it wraps past midnight, which is the normal case for a night
 * profile: 23:00-07:00 is an hour at the right-hand end and seven at the left,
 * not a negative-width bar.
 */
/** The four hours the track marks and the axis labels. Every six hours: the
 *  quarters of the day, and enough to place a slab without crowding a 160px
 *  column. */
const DAY_TICKS = [0, 6, 12, 18]

function arcs([from, to]) {
  const pct = (minute) => (minute / MINUTES_IN_DAY) * 100
  if (from < to) return [{ left: pct(from), width: pct(to - from) }]
  return [
    { left: pct(from), width: pct(MINUTES_IN_DAY - from) },
    { left: 0, width: pct(to) },
  ]
}
