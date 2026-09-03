import ScrollableTable from './ScrollableTable'
import {
  attendanceFor,
  describeAttendance,
  suggestedAttendance,
  unansweredAttendance,
} from '../utils/plannerNpc'
import { coversSmallHours, windowDayShare, dispatchWindowFor, MINUTES_IN_DAY } from '../utils/plannerClock'

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
 * Data in, callbacks out, and no effects: it renders from props alone so
 * `renderToString` reaches all of it, unlike the stage it sits in.
 */
export default function DayNightPanel({
  profileNames,
  activeProfile,
  profileWindows,
  profileAttendance,
  attendanceRequired,
  reservedWindow,
  onWindow,
  onAttendance,
  onReservedWindow,
  onSelectProfile,
}) {
  const withHours = profileNames.filter((name) => dispatchWindowFor(profileWindows[name]) != null)
  const unanswered = attendanceRequired ? unansweredAttendance(withHours, profileAttendance) : []
  // Every profile awake, and one of them running through the small hours. Not
  // an error -- a night shift is a real thing -- but it is the shape of a
  // forgotten answer, and the consequence is a night route funded by trading
  // nobody did.
  const claimsAwakeAtNight = withHours.filter(
    (name) =>
      attendanceFor(profileAttendance, name) === true && coversSmallHours(profileWindows[name])
  )

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

      {claimsAwakeAtNight.length > 0 && (
        <p className="text-warning text-xs mb-3">
          {claimsAwakeAtNight.join(', ')} runs through the small hours and says you are
          trading. If you are asleep then, say so — the allowance would otherwise fund
          cargo out of conversions nobody performs.
        </p>
      )}

      <ScrollableTable>
        <table className="w-full text-xs">
          <thead className="text-secondary uppercase">
            <tr>
              <th className="text-left py-1 px-2 sticky-col">Profile</th>
              <th className="text-left px-2">Runs</th>
              <th className="text-left px-2">Hours of the day</th>
              <th
                className="text-left px-2"
                title="Whether you are at the marketplace during these hours, so section 7's NPC conversion can actually be performed. Required as soon as any village keeps a stock floor: false means the conversion allowance is zero for this profile — the crop keeps growing, but nobody is converting it."
              >
                Who is trading
              </th>
            </tr>
          </thead>
          <tbody>
            {profileNames.map((name) => {
              const hours = profileWindows[name] ?? null
              const pair = dispatchWindowFor(hours)
              const share = windowDayShare(hours)
              const answer = attendanceFor(profileAttendance, name)
              const suggestion = suggestedAttendance(hours)
              const owed = attendanceRequired && pair != null && answer === null
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
                        <span
                          aria-hidden="true"
                          className="block h-2 rounded-full relative overflow-hidden"
                          style={{ background: 'var(--md-surface-container-high)' }}
                        >
                          {arcs(pair).map((arc, i) => (
                            <span
                              key={i}
                              className="absolute top-0 h-full"
                              style={{
                                left: `${arc.left}%`,
                                width: `${arc.width}%`,
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
                      aria-describedby={owed ? problemId : undefined}
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
                    <span className="block text-secondary mt-0.5">
                      {describeAttendance(answer)}
                    </span>
                    {owed && (
                      <>
                        <span id={problemId} className="block text-warning mt-0.5">
                          the plan is refused until this says
                        </span>
                        {/* The clock's guess, offered rather than applied. One
                            press, and the label names the answer it will
                            write so it is never a mystery button. */}
                        <button
                          type="button"
                          className="block underline text-info hover:text-primary mt-0.5 pointer-coarse:min-h-11"
                          onClick={() => onAttendance(name, suggestion)}
                        >
                          These hours look like{' '}
                          {suggestion ? 'you are awake' : 'you are asleep'} — say so
                        </button>
                      </>
                    )}
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
function arcs([from, to]) {
  const pct = (minute) => (minute / MINUTES_IN_DAY) * 100
  if (from < to) return [{ left: pct(from), width: pct(to - from) }]
  return [
    { left: pct(from), width: pct(MINUTES_IN_DAY - from) },
    { left: 0, width: pct(to) },
  ]
}
