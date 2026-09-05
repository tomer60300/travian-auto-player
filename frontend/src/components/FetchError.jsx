/**
 * The persistent "this read failed" box.
 *
 * The wave-4 census's most common defect, on 7 of 15 pages: a fetch failure and
 * a genuinely empty result rendered byte-for-byte the same text, so "the game
 * is idle" and "we could not ask the game" were indistinguishable. Two of them
 * were worse than that -- Buildings' queue section vanished entirely on
 * failure, and Reports rendered nothing at all, not even its own empty
 * sentence.
 *
 * A toast is not a fix: it expires, and the census confirmed live that 4.5s
 * later the page reads exactly like the empty state again. This box stays.
 *
 * `.result-box result-box-danger` is the surface `Military.jsx` and
 * `VideoRewards.jsx` already use for exactly this and that the census called
 * best-in-class; it is `--md-error-container`/`--md-on-error-container`, so it
 * flips with the theme. `role="alert"` so a screen reader is told without
 * having to find it, the same way `Login.jsx` announces its failure.
 *
 * Props:
 *   what      — what failed, in words, always rendered.
 *   detail    — the server's own message, when there is one.
 *   onRetry   — omit when there is nothing the user can do but navigate away.
 *   retryLabel
 */
export default function FetchError({ what, detail, onRetry, retryLabel = 'Retry' }) {
  return (
    <div className="result-box result-box-danger flex flex-wrap items-center gap-3" role="alert">
      <span className="flex-1 min-w-[12rem]">
        {what}
        {detail ? ` — ${detail}` : ''}
      </span>
      {onRetry && (
        <button className="btn-secondary btn-sm" onClick={onRetry}>
          {retryLabel}
        </button>
      )}
    </div>
  )
}
