/**
 * A loading placeholder that occupies the height its content will occupy.
 *
 * Item 6 of the UI Definition of Done is "no layout shift as data arrives --
 * reserve the space, or skeleton it", and a centred spinner does neither: it is
 * one line tall whatever arrives afterwards, so the whole page below it jumps
 * when the rows land. Measured at 768 before this existed, one fetch moved the
 * panels under it by 200px on BuildQueue and 180px on FarmLists.
 *
 * `rows` x `height` is the reservation, so a caller sizes it from the row it is
 * standing in for rather than from a guess. `.skeleton` is the app's own
 * shimmer, which `@media (prefers-reduced-motion: reduce)` already stills.
 *
 * `aria-hidden` with a `role="status"` label beside it: a screen reader should
 * hear "Loading X", not eight anonymous boxes.
 */
export default function SkeletonRows({ rows = 8, height = 44, gap = 4, label = 'Loading' }) {
  return (
    <div className="flex flex-col" style={{ gap }}>
      <span className="sr-only" role="status">
        {label}
      </span>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ height }} aria-hidden="true" />
      ))}
    </div>
  )
}
