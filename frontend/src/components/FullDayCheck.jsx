import NightOverrunTable from './NightOverrunTable'
import ScrollableTable from './ScrollableTable'

const RESOURCE_LABEL = { lumber: 'Lumber', clay: 'Clay', iron: 'Iron', crop: 'Crop' }

const fmt = (n) => (n == null ? '—' : Math.round(n).toLocaleString())
const signed = (n) => (n == null ? '—' : `${n > 0 ? '+' : ''}${Math.round(n).toLocaleString()}`)

/** What one profile leaves the next, measured across a repeating day.
 *
 * Profiles are planned separately; the account lives through all of them. This
 * replays the composite and reports the four things section 6 actually asks
 * about, each of which was computed by the backend and rendered nowhere:
 *
 *   * **the morning floor** — 60% of both stores at the day switch, a floor the
 *     plan must ACHIEVE, reported emptiest first;
 *   * **the pre-night baseline** — no more than 25% at the night switch, an
 *     assumption the operator establishes by hand, reported fullest first;
 *   * **night overruns** — routes still carrying merchants at 07:00;
 *   * the crop swing per village, which was already here.
 *
 * Both fills are FRACTIONS, 0 to 1, and both are shown against their threshold
 * rather than as a bare percentage: "42%" says nothing without the 60% it
 * missed, and the operator's whole question is by how much. So each row is a
 * bar with the threshold marked on it, the two figures beside it, and the gap
 * named in words — a bar alone would carry the answer in geometry only.
 *
 * The two thresholds are the server's own (`morning_floor`,
 * `pre_night_baseline`), never restated here: they are the same pair the night
 * derivation uses as its baseline and ceiling, and a second copy in the view
 * would let the panel disagree with the plan it is describing.
 */
export default function FullDayCheck({ dayCheck, dayChecking, onRun, cropCeilings, villages }) {
  const morningFloor = dayCheck?.morning_floor ?? null
  const preNightBaseline = dayCheck?.pre_night_baseline ?? null

  return (
    <div className="card p-4">
      <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
        <div className="min-w-[18rem] flex-1">
          <h3 className="font-semibold">The whole day, every profile in its hours</h3>
          <p className="text-secondary text-xs mt-0.5">
            Profiles are planned separately, but the account lives through all of them: what
            the day ships decides the stock the night starts from. This simulates the
            composite — net rates per window, production always on — and answers what one
            profile leaves the next.
          </p>
        </div>
        <button
          type="button"
          className="btn-primary text-xs py-1.5"
          disabled={dayChecking}
          onClick={onRun}
        >
          {dayChecking ? 'Simulating…' : dayCheck ? 'Re-run (0 requests)' : 'Run (0 requests)'}
        </button>
      </div>

      {dayCheck?.skipped?.length > 0 && (
        <p className="text-warning text-xs mb-2">
          Skipped {dayCheck.skipped.join(', ')} — no hours set. Give each profile its window
          in the table above.
        </p>
      )}

      {dayCheck?.warnings?.length > 0 && (
        <div className="mb-3">
          <p className="text-xs text-warning font-semibold">
            ⚠ Warnings ({dayCheck.warnings.length})
          </p>
          <ul className="text-xs text-warning list-disc list-inside space-y-0.5">
            {dayCheck.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}
      {dayCheck && dayCheck.warnings.length === 0 && (
        <p className="text-success text-xs mb-3">
          No store crosses its cap, alert level or zero across the full day.
        </p>
      )}

      {/* Section 6's two state rules, one from each end of the night. Rendered
          as a pair even when only one of them has rows, because "nothing to
          report" about a threshold the operator has never seen reads as a
          missing feature rather than as a pass. */}
      {dayCheck && (
        <div className="grid gap-3 md:grid-cols-2 mb-3">
          <FillSection
            title="Empty at the morning switch"
            threshold={morningFloor}
            rows={dayCheck.morning_shortfalls ?? []}
            direction="below"
            clear="Every role village reaches the morning floor on every store."
            explain={
              <>
                Section 6 asks every role village — DEF and both off villages, the capital
                excluded — to hold at least{' '}
                <strong>{pct(morningFloor)}</strong> of each store when the day profile takes
                over, so the morning starts with something to spend. Measured where the plan{' '}
                <span className="text-primary">leaves</span> them on a repeating day, not
                where the snapshot found them. Emptiest first.
              </>
            }
          />
          <FillSection
            title="Still full at the night switch"
            threshold={preNightBaseline}
            rows={dayCheck.pre_night_over_baseline ?? []}
            direction="above"
            clear="Every role village is down to the night baseline on every store."
            explain={
              <>
                The night profile <span className="text-primary">assumes</span> each store is
                down to <strong>{pct(preNightBaseline)}</strong> by the time it starts — you
                spend them down by hand, so this is a trusted starting condition and never a
                refusal. A store still above it means the night has less room than the
                derivation gave it. Fullest first.
              </>
            }
          />
        </div>
      )}

      {/* Section 6's third rule: everything home by 07:00. The same table the
          Plan stage renders for a single profile -- one implementation, so the
          two surfaces cannot describe the same rows differently. */}
      {dayCheck && (
        <NightOverrunTable
          className="border-t-default pt-2 mb-3"
          rows={dayCheck.night_overruns ?? []}
          emptyNote="Every night movement closes before the morning profile starts, so it begins with a full merchant pool everywhere."
        />
      )}

      {dayCheck && (
        <ScrollableTable>
          <table className="w-full text-xs">
            <thead className="text-secondary uppercase">
              <tr>
                <th className="text-left py-1 px-2 sticky-col">Village</th>
                <th className="text-right px-2">Crop now</th>
                <th className="text-right px-2">Day swing (low → high)</th>
                <th className="text-right px-2">Drift/day</th>
                <th className="text-right px-2" title="Your alert level from the Account stage">
                  Alert at
                </th>
              </tr>
            </thead>
            <tbody>
              {dayCheck.villages
                .filter((t) => t.resource === 'crop')
                .map((t) => {
                  const ceiling = Number(cropCeilings[t.village_id]) || null
                  const nearAlert = ceiling != null && t.high >= ceiling
                  return (
                    <tr
                      key={t.village_id}
                      className="row-focus touch-target border-t-default"
                      /* A token wash, not `bg-danger/10`: there is no `@theme`
                         block in index.css, so Tailwind never generates an
                         opacity variant of a semantic class and a bare
                         `bg-danger/10` renders nothing at all. */
                      style={
                        nearAlert
                          ? {
                              backgroundColor:
                                'color-mix(in srgb, var(--danger) 10%, transparent)',
                            }
                          : undefined
                      }
                    >
                      <td className="py-1 px-2 sticky-col row-focus-edge">
                        {t.village_name}
                        {/* Named as well as tinted: a red wash is not a
                            statement, and this row is the one that matters. */}
                        {nearAlert && (
                          <span className="block text-danger text-[10px] uppercase">
                            crosses your alert
                          </span>
                        )}
                      </td>
                      <td className="text-right px-2 font-mono text-secondary">
                        {fmt(villages.find((v) => v.village_id === t.village_id)?.crop_stock ?? 0)}
                      </td>
                      <td className="text-right px-2 font-mono">
                        {fmt(t.low)} → {fmt(t.high)}
                        {!t.settled && (
                          <span
                            className="text-warning ml-1"
                            title="Still drifting at the simulation horizon — the drift column is the story"
                          >
                            ↗
                          </span>
                        )}
                      </td>
                      <td
                        className={`text-right px-2 font-mono ${
                          // Either direction of drift needs attention: up walks
                          // into the cap or the alert, down walks toward an
                          // empty granary. Green would read as "fine" about a
                          // village slowly starving.
                          Math.abs(t.daily_net) < 1 ? 'text-secondary/60' : 'text-warning'
                        }`}
                      >
                        {signed(t.daily_net)}
                      </td>
                      <td className="text-right px-2 font-mono text-secondary">
                        {ceiling != null ? fmt(ceiling) : '—'}
                      </td>
                    </tr>
                  )
                })}
            </tbody>
          </table>
        </ScrollableTable>
      )}

      {!dayCheck && !dayChecking && (
        <p className="text-secondary text-xs italic">
          Not run yet. It costs no game requests — everything comes from the snapshot you
          already hold.
        </p>
      )}
      <p className="text-secondary text-[11px] mt-2">
        Cargo is counted when it <em>lands</em>, not when it leaves: a batch a day-profile
        route dispatches at 22:00 is credited to whichever profile owns the hour it actually
        arrives in. Each profile’s routes fire on their own schedule inside its hours, so an
        overflow caused by a hand-off between profiles shows up here.
      </p>
    </div>
  )
}

/** A fraction as a percentage, or an em-dash. The wire carries 0..1. */
function pct(fraction) {
  return fraction == null ? '—' : `${Math.round(fraction * 100)}%`
}

/** One of section 6's two switch rules, as rows against their threshold. */
function FillSection({ title, threshold, rows, direction, clear, explain }) {
  return (
    <div className="border-t-default pt-2">
      <p className="text-xs font-semibold">
        {title}{' '}
        <span className="text-secondary font-normal">
          · threshold {pct(threshold)}
          {rows.length > 0 ? ` · ${rows.length} store${rows.length === 1 ? '' : 's'}` : ''}
        </span>
      </p>
      <p className="text-secondary text-[11px] mt-1">{explain}</p>
      {rows.length === 0 ? (
        <p className="text-success text-xs mt-2">{clear}</p>
      ) : (
        <ul className="mt-2 space-y-1.5">
          {rows.map((row) => (
            <li key={`${row.village_id}-${row.resource}`} className="text-xs">
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate">
                  {row.village_name}{' '}
                  <span className="text-secondary">
                    {RESOURCE_LABEL[row.resource] ?? row.resource} · {row.store}
                  </span>
                </span>
                <span className={`font-mono shrink-0 ${direction === 'below' ? 'text-warning' : 'text-warning'}`}>
                  {pct(row.fill)}
                </span>
              </div>
              <FillBar fill={row.fill} threshold={threshold} direction={direction} />
              <div className="text-secondary text-[11px] font-mono">
                {fmt(row.stock)} of {fmt(row.capacity)}
                {threshold != null && (
                  <>
                    {' · '}
                    {direction === 'below'
                      ? `${fmt(threshold * row.capacity - row.stock)} short of the floor`
                      : `${fmt(row.stock - threshold * row.capacity)} over the baseline`}
                  </>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** The measurement and its threshold on one track.
 *
 * `role="img"` with the same two numbers in its label, because the geometry is
 * the only thing the bar adds and a screen reader gets nothing from a marker
 * position. Decorative in the strict sense — the figures are printed either
 * side of it — but naming it costs one attribute and a bar with no name reads
 * as an unlabelled graphic.
 */
function FillBar({ fill, threshold, direction }) {
  const width = Math.max(0, Math.min(1, Number(fill) || 0)) * 100
  const mark = threshold == null ? null : Math.max(0, Math.min(1, threshold)) * 100
  return (
    <span
      role="img"
      aria-label={`${Math.round(width)}% full against a ${
        mark == null ? 'no' : `${Math.round(mark)}%`
      } threshold`}
      className="block h-1.5 my-0.5 rounded-full relative overflow-hidden"
      style={{ background: 'var(--md-surface-container-high)' }}
    >
      <span
        className="absolute top-0 left-0 h-full"
        style={{
          width: `${width}%`,
          background: direction === 'below' ? 'var(--warning)' : 'var(--info)',
        }}
      />
      {mark != null && (
        <span
          className="absolute top-0 h-full"
          style={{ left: `${mark}%`, width: '2px', background: 'var(--text-primary)' }}
        />
      )}
    </span>
  )
}
