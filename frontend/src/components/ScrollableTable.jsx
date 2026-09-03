import { useEffect, useLayoutEffect, useRef, useState } from 'react'

/**
 * `useLayoutEffect` in a browser, `useEffect` on a server render.
 *
 * The distinction is load bearing here rather than idiomatic noise, and it was
 * measured both ways. `useEffect` runs AFTER paint, so the table painted
 * unpinned and un-hinted for one frame and the hint's arrival shifted
 * everything below it: CLS attributed to the scroll container was 0.0047 at
 * 375, 0.0097 at 768 and 0.0064 at 1440. `useLayoutEffect` runs before paint,
 * the ResizeObserver's first delivery is dispatched in the same frame, and the
 * first frame the operator sees already has the pinning and the hint.
 *
 * The switch is because `renderToString` runs no effects and warns about this
 * one specifically -- and every vitest suite in this repo renders that way, one
 * of them without a `console.error` mock. `window` is the environment test
 * React itself uses for the warning.
 */
const useMeasureEffect = typeof window === 'undefined' ? useEffect : useLayoutEffect

/**
 * A dense editor table that scrolls sideways inside its own box, pinning its
 * identity column and telling the operator the rest is there — but only while
 * it actually overflows.
 *
 * Both of those used to be guesses at a viewport width, and both guesses were
 * wrong. `.sticky-col` was gated on `@media (max-width: 640px)` with a comment
 * claiming "at desktop widths the table does not overflow", and the hint was
 * `sm:hidden`. Measured against the running app: the Snapshot table's
 * intrinsic width is 1408px, in a 470px container at 768 and a 1122px
 * container at 1440. So at 768 the identity cell was `position: static`,
 * scrolled to left -688 — off screen — and the hint was gone too, leaving the
 * operator to scroll 938px with no village pinned and no warning that ten
 * columns existed. A Trade Office level typed one row off breaches that
 * village's merchant budget with nothing on screen saying so.
 *
 * The other direction is a lie as well: the foreign-targets table and the
 * Allocate grid both fit at 1440, so a hint that always showed would claim to
 * hide columns it was displaying, and a permanent pinned hairline would mark a
 * boundary that means nothing.
 *
 * So the question is asked of the geometry instead — `scrollWidth >
 * clientWidth` on the container itself — and one answer drives both the
 * pinning (`.table-overflowing .sticky-col` in index.css) and the hint. No
 * breakpoint to keep in sync with a column count, and adding a column cannot
 * make either of them stale.
 *
 * A component rather than a bare hook because the Allocate grid renders one of
 * these per resource inside `RESOURCES.map(...)`, where a hook cannot be
 * called, and because the container, its class and its hint have to agree —
 * they were three separate edits at four call sites before.
 *
 * `position: relative` is not decoration: an `overflow-x: auto` ancestor does
 * not clip an absolutely positioned descendant it is not the containing block
 * for. Six `.sr-only` spans inside the Snapshot table sat at x=1261 in a 375
 * viewport and slid the whole document sideways until this wrapper became
 * their containing block.
 *
 * @param {string} hint - what the operator is told when the table overflows.
 *   Name the columns that are off-screen, in the order they appear: listing
 *   them out of order describes a table that does not exist, which is worse
 *   than saying nothing because the reader counts across to the wrong column
 *   and types a figure into it.
 * @param {string} [className] - extra classes for the scroll container.
 */
export default function ScrollableTable({ hint, className = '', children }) {
  const ref = useRef(null)
  const [overflowing, setOverflowing] = useState(false)

  useMeasureEffect(() => {
    const node = ref.current
    if (node == null) return
    const measure = () => setOverflowing(node.scrollWidth > node.clientWidth)
    // Measured HERE, synchronously, and not left to the observer's first
    // callback. `observe()` queues that callback for the next rendering cycle,
    // so the frame this effect belongs to still painted an unpinned table with
    // no hint, and the hint's arrival one frame later was a real shift: CLS
    // attributed to this container was 0.0047 at 375, 0.0097 at 768 and 0.0064
    // at 1440, and suppressing the hint took each of them to zero. A setState
    // in a layout effect is flushed before paint, which is why this is a layout
    // effect. Item 6 of the UI Definition of Done.
    measure()
    // From here the observer is for CHANGES only.
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    // The container's own box does not change when a COLUMN does, so the table
    // inside it is observed as well: adding a foreign-target row or filtering
    // the village list changes the content width without touching the
    // container.
    for (const child of node.children) observer.observe(child)
    return () => observer.disconnect()
  }, [])

  return (
    <>
      {overflowing && <p className="scroll-hint text-secondary text-xs mb-1">{hint}</p>}
      <div
        ref={ref}
        className={`relative overflow-x-auto${overflowing ? ' table-overflowing' : ''} ${className}`}
      >
        {children}
      </div>
    </>
  )
}
