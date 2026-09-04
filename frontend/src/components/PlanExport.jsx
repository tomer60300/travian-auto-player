import { planDigestShort } from '../utils/plannerExport'

/** Section 10's output order, as a control: read the plan, confirm it, get the file.
 *
 * *"Output order: readable plan first → operator confirms → then generate
 * YAML/code."* The confirmation is not a dialog and deliberately so -- a dialog
 * confirms that a button was pressed, not that a particular plan was read.
 * `/plan` returns a `plan_digest` over the response it displayed, this button
 * sends that digest back with the request, and the server re-plans and refuses
 * with a **409** unless the two agree. So the file either IS the plan that was
 * confirmed, or it does not exist.
 *
 * The digest is on screen because that is what makes the mechanism checkable
 * rather than magic: the twelve characters here are the twelve in the
 * downloaded file's name, so an operator holding three exports can tell which
 * plan each one describes.
 *
 * A conflict is rendered IN THE PAGE and never retried. Two decisions, both
 * deliberate:
 *
 *   * Not a toast, because this needs an action and a toast is gone before the
 *     operator has decided what to do about it.
 *   * Not a retry, because re-planning to make the download succeed is exactly
 *     the outcome the digest exists to prevent -- an authoritative-looking file
 *     describing a plan nobody read. The way out is to re-read the plan, which
 *     is offered as its own button and says what it does.
 */
export default function PlanExport({ digest, exporting, conflict, onConfirm, onRePlan }) {
  return (
    /* No `.card` of its own any more: the planner renders this inside a
       disclosure that IS the card, and a card inside a card reads as two
       panels. */
    <div>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="min-w-[18rem] flex-1">
          <h3 className="font-semibold">Confirm this plan, then export it</h3>
          <p className="text-secondary text-xs mt-0.5">
            The readable plan above comes first; the document comes after you confirm it.
            Confirming sends this plan&apos;s identity back with the request, and the server
            renders the file only if it still re-plans to the same thing — so the YAML you
            keep is the plan you actually read, or there is no file at all.
          </p>
          <p className="text-secondary text-xs mt-1 font-mono">
            plan{' '}
            <span className="text-info" title={digest || 'no digest'}>
              {planDigestShort(digest)}
            </span>{' '}
            <span className="text-secondary">— the same twelve the file is named after</span>
          </p>
        </div>
        {/* No `whitespace-nowrap`, and that is measured rather than stylistic:
            this label is 46 characters, and held on one line it came to 350px
            starting at x=41 in a 375 viewport -- 16px of DOCUMENT scroll,
            which is item 1 of the UI Definition of Done. The card's own
            `flex-wrap` already gives the button its own line at that width;
            what it cannot do is make the label narrower than one word. */}
        {/* `btn-secondary`, not `btn-primary`. This was the biggest filled
            button on the whole planner -- for a DOCUMENT, which changes nothing
            in the game -- while the button that writes to a real account was a
            small one further down. The write path owns the loud button now. */}
        <button
          type="button"
          className="btn-secondary text-xs py-1.5"
          disabled={exporting || !digest}
          onClick={onConfirm}
        >
          {exporting ? 'Rendering…' : 'Confirm this plan and export YAML · 0 requests'}
        </button>
      </div>

      {conflict && (
        <div className="mt-3 border-t-default pt-3">
          {/* `role="alert"` rather than `status`: the operator asked for a file
              and did not get one, and the reason changes what they should do
              next. */}
          <p className="text-danger text-xs" role="alert">
            <strong>The plan moved since you read it.</strong> Nothing was rendered — a
            document describing a plan nobody read is worse than no document, so the export
            was refused rather than quietly re-planned.
          </p>
          {/* The server's own sentence, which names BOTH digests. "It moved"
              without saying from what to what is not something anyone can
              check, and this is the one place those two strings exist. */}
          <p className="text-secondary text-xs mt-1 font-mono break-all">{conflict}</p>
          <button type="button" className="btn-secondary btn-xs mt-2" onClick={onRePlan}>
            Re-read the plan (0 requests)
          </button>
        </div>
      )}
    </div>
  )
}
