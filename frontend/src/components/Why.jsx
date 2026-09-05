/** A field's reasoning, one click away.
 *
 * The controlled-run box's four checkbox labels measured 489, 408, 215 and 300
 * characters, and the two number inputs sat BETWEEN two of those paragraphs.
 * The bolded lead clause of each was already the label; the rest is prose that
 * carries real warnings, so it is disclosed rather than deleted -- the same
 * mechanism this page uses in nine other places.
 *
 * A `<button>` inside `<summary>` would be a control inside a control, so this
 * is the native disclosure and the SUMMARY carries the accessible name. It
 * still exposes `role="button"` to the accessibility tree, which is how a test
 * and a screen reader both reach it by name.
 *
 * `pointer-coarse:min-h-11`/`min-w-11` because a "?" glyph is about seven
 * pixels wide, and item 4 of the UI Definition of Done wants 44px on a coarse
 * pointer. The desktop layout is untouched: the constraint only applies there.
 *
 * Its own file since the day-check panel needed one too: `FullDayCheck` is
 * imported BY `ResourcePlanner`, so importing the component back out of that
 * page would be a cycle, and a second copy of a control this page uses in ten
 * places is how two disclosures end up with two focus rings.
 */
export default function Why({ label, children }) {
  return (
    <details className="text-xs inline-block align-top">
      <summary
        className="why-toggle cursor-pointer list-none inline-flex items-center justify-center rounded-full border-default text-secondary hover:text-primary w-4 h-4 leading-none pointer-coarse:min-h-11 pointer-coarse:min-w-11"
        aria-label={`Why: ${label}`}
        title={`Why: ${label}`}
      >
        ?
      </summary>
      <div className="text-secondary mt-1 max-w-md">{children}</div>
    </details>
  )
}
