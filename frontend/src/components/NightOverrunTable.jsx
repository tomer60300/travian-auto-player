import ScrollableTable from './ScrollableTable'

/** Routes still carrying merchants when the day profile takes over.
 *
 * Section 6: all night movements complete before 07:00, so the morning starts
 * with a full merchant pool everywhere. The arithmetic is shown rather than
 * asserted -- the last departure the profile makes, the round trip that follows
 * it, and by how much the two overrun -- because "this route overruns" is not
 * something an operator can act on and "the 06:00 firing gets home at 07:48"
 * is.
 *
 * ONE implementation for two callers. The backend reports the same rows on
 * `PlanResponse` and on `DayCheckResponse`, and they are the same fact seen
 * from a single profile and from the composite day; two renderers would be two
 * chances to describe them differently. `emptyNote` is the only thing that
 * differs, because "no night movement is late" means something slightly
 * different when you are looking at one profile than at the whole day.
 */
export default function NightOverrunTable({ rows, emptyNote, className = '' }) {
  return (
    <div className={className}>
      <p className="text-xs font-semibold">
        Still on the road at the switch{' '}
        <span className="text-secondary font-normal">
          {rows.length === 0 ? '· none' : `· ${rows.length} route${rows.length === 1 ? '' : 's'}`}
        </span>
      </p>
      {rows.length === 0 ? (
        <p className="text-success text-xs mt-1">{emptyNote}</p>
      ) : (
        <>
          <p className="text-danger text-xs mt-1">
            These merchants are still out when the day profile takes over, so the morning
            starts short of the fleet its own routes were costed against.
          </p>
          <ScrollableTable label="Night movements still on the road at the switch">
            <table className="w-full text-xs mt-1">
              <thead className="text-secondary uppercase">
                <tr>
                  <th className="text-left py-1 px-2 sticky-col">Route</th>
                  <th className="text-right px-2">Cycle</th>
                  <th
                    className="text-right px-2"
                    title="The LAST departure inside the profile's hours, which is the firing the deadline binds on. A route fires 24/N times a day and the others all have more room."
                  >
                    Last send
                  </th>
                  <th
                    className="text-right px-2"
                    title="Out and back, unrounded. A merchant is committed for the whole journey, so a delivery landing at 06:00 from an hour out still has merchants on the road at 07:00."
                  >
                    Round trip
                  </th>
                  <th className="text-right px-2">Overrun</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={`${row.origin}-${row.destination}-${row.last_dispatch_minute}`}
                    className="row-focus touch-target border-t-default"
                  >
                    <td className="py-1 px-2 sticky-col row-focus-edge whitespace-nowrap">
                      {row.origin_name} → {row.destination_name}
                    </td>
                    <td className="text-right px-2 font-mono">{row.cycle_hours}h</td>
                    <td className="text-right px-2 font-mono">{row.last_dispatch_clock}</td>
                    <td className="text-right px-2 font-mono">
                      {(row.round_trip_minutes / 60).toFixed(1)}h
                    </td>
                    <td className="text-right px-2 font-mono text-danger">
                      {/* Named in a unit, not just coloured: a bare "+108" in
                          red says something is wrong without saying what. */}
                      {Math.round(row.overrun_minutes)}m late
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollableTable>
        </>
      )}
    </div>
  )
}
