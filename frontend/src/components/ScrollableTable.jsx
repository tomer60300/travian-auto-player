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
 * A column's name, as a reader would say it.
 *
 * `[aria-hidden="true"]` children are dropped because that attribute already
 * declares them decoration: the sort controls in the Snapshot table's headers
 * append an arrow glyph, and `Village↕` is not the name of a column.
 */
const columnName = (th) => {
  const clone = th.cloneNode(true)
  for (const decoration of clone.querySelectorAll('[aria-hidden="true"]')) decoration.remove()
  return clone.textContent.replace(/\s+/g, ' ').trim()
}

/**
 * What is off to the right, read off the header row that is actually there.
 *
 * The pinned column is the one the operator keeps, so it is named as staying
 * rather than counted as leaving. Header cells with no text — the action
 * column at the end of the Role-templates and foreign-target tables — are not
 * columns anyone can be told to scroll to.
 */
const describe = (node) => {
  const heads = [...node.querySelectorAll('thead tr:first-child th')]
  const pinned = heads.find((th) => th.classList.contains('sticky-col'))
  const scrolling = heads
    .filter((th) => th !== pinned)
    .map(columnName)
    .filter((name) => name !== '')
  if (scrolling.length === 0) return ''
  const count = scrolling.length === 1 ? '1 more column' : `${scrolling.length} more columns`
  const span =
    scrolling.length === 1
      ? scrolling[0]
      : `${scrolling[0]} to ${scrolling[scrolling.length - 1]}`
  const stays = pinned == null ? '.' : ` — ${columnName(pinned)} stays pinned.`
  return `Scroll sideways for ${count}, ${span}${stays}`
}

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
 * The hint's WORDS come off the table's own header row rather than a prop, for
 * the same reason its VISIBILITY comes off the measured overflow rather than a
 * breakpoint: a hand-typed description of a table is stale the moment a column
 * is added, and nobody maintains it. The one over the Snapshot table had
 * already gone stale — it listed "Merchants, Trade Office, Crop alert, Ships
 * only to, Relays for, Stock floor and Consumption" for a table that runs
 * Merchants / ROLE / Trade Office / MAX BUSY / Crop alert / …, so an operator
 * counting across from Merchants to Trade Office landed on Role, which is a
 * 233px editable select. Round 9 promoted that hint from a 375-only paragraph
 * to the primary cue at every viewport, so the miscount travelled with it.
 * The Allocate grid's omitted Own/h the same way, and neither the
 * foreign-target nor the Role-template hint named a single header verbatim.
 *
 * Naming every column is what cannot be kept true, so the derived form names
 * the ends and the count instead: they all move together when a column is
 * added, and none of them can be typed wrong.
 */
export default function ScrollableTable({ label, children }) {
  const ref = useRef(null)
  const [{ overflowing, hint }, setState] = useState({ overflowing: false, hint: '' })

  useMeasureEffect(() => {
    const node = ref.current
    if (node == null) return
    const measure = () => {
      const overflows = node.scrollWidth > node.clientWidth
      setState((prev) => {
        const next = { overflowing: overflows, hint: overflows ? describe(node) : '' }
        return prev.overflowing === next.overflowing && prev.hint === next.hint ? prev : next
      })
    }
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
      {/* Reachable by Tab, and only while there is something out of reach.
          WCAG 2.1.1: an `overflow-x: auto` box is scrolled by the pointer and
          by nothing else. Tabbing into an off-screen INPUT auto-scrolls it into
          view, which is why this went unnoticed for twelve rounds -- but the
          read-only figures and the whole header row in the scrolled-away region
          are not focusable, so a keyboard user could not reach them at all. On
          the Account table that is every production rate and every column
          heading past the pinned village.

          Gated on `overflowing` for the same reason the hint and the pinning
          are: a tab stop on a container with nothing hidden is a stop that does
          nothing, and there are ten of these on the page.

          `role="region"` needs a name to be a landmark at all, so the name is a
          prop. It is the ONE thing here that is hand-written rather than
          derived, and deliberately: what the hint says about a table has to
          track its columns, and what a region is CALLED is its identity. The
          derived form would be the pinned column's heading, which is "Village"
          on five of them. */}
      <div
        ref={ref}
        role={overflowing ? 'region' : undefined}
        aria-label={overflowing ? label : undefined}
        tabIndex={overflowing ? 0 : undefined}
        className={`relative overflow-x-auto${overflowing ? ' table-overflowing' : ''}`}
      >
        {children}
      </div>
    </>
  )
}
