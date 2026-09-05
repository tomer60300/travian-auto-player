/** Where the operator's typed setup lives: a file, and now the server too.
 *
 * `localStorage` is scoped to an ORIGIN, and this app is reached on four of
 * them -- :80, :8001, the LAN address and over Tailscale -- so it kept four
 * independent copies of every Trade Office level, role, relay tier and spend,
 * and a cleared origin lost the lot. Export-to-file was the workaround. This is
 * the shared copy, and the file stays: a server round trip needs the server,
 * and the file is what survives one being rebuilt.
 *
 * **404 and an empty setup are different states, and this panel is where that
 * distinction is spent.** The server returns 404 when nothing has ever been
 * saved, deliberately: "you have never saved" is an invitation to import a
 * file, while "you saved a blank sheet" is a decision to leave the account
 * undescribed. Collapsing the two into "no setup" would turn the second into
 * the first and quietly suggest the operator undo it. So the line above the
 * buttons says which one it is, in words, before anything is pressed -- and it
 * is probed rather than waited for, because an operator arriving on a fourth
 * origin needs to know a saved copy exists without first guessing that it might.
 *
 * One document, not two formats: what is stored is exactly what the file export
 * writes, so a document can move between the two paths and the server validates
 * it with the plan request's OWN rules.
 */
export default function SetupStorage({
  status,
  busy,
  blockers = [],
  onSave,
  onLoad,
  onForget,
  onExportFile,
  onImportFile,
  onPaste,
  pasteOpen,
}) {
  // The two WRITERS are held back while a cell is marked; the three readers are
  // not. `Build plan` has refused a marked cell since `plannerBlockers.js`, and
  // these were gated on `busy` alone -- so an even `map_span` or a `0` speed
  // saved with a 200 and could never be loaded again, because `parseSetup`
  // refuses both on the way back in and refuses the whole file rather than
  // half-loading it. That leaves the operator's only backup unreadable by their
  // own build, which is worse than not having saved.
  //
  // A load is the way OUT of a bad state, so gating one would strand the
  // operator in it.
  const blocked = blockers.length > 0
  const blockedId = 'setup-save-blocked'
  return (
    <div className="border-t-default pt-3 mt-3">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="min-w-[16rem] flex-1">
          <p className="text-secondary text-xs uppercase">Your typed setup</p>
          <p className="text-secondary text-xs mt-0.5">
            Roles, Trade Office levels, merchant caps, the relay tier, stock floors and
            spends are all typed by hand and stored per browser origin — so they do not
            follow you between <span className="font-mono">:80</span>,{' '}
            <span className="font-mono">:8001</span>, the LAN address and Tailscale. Keep
            them on the server so all four agree, and in a file so they survive one being
            rebuilt.
          </p>
          {/* The state in a sentence, before anything is pressed. */}
          <p className={`text-xs mt-1 ${statusTone(status.state)}`} role="status">
            {describeStatus(status)}
          </p>
          {/* And the reason the two writers are off, in the cell's own words.
              A disabled button with nothing beside it is the defect this
              replaces: the operator presses Save, nothing happens, and nothing
              says why. Named to a screen reader through `aria-describedby` on
              both buttons, not only shown. */}
          {blocked && (
            <p id={blockedId} className="text-warning text-xs mt-1" role="status">
              Saving is held back while a figure on this page is outside what the plan
              accepts, because this build would refuse to read the document back:{' '}
              {blockers.map(describeBlockedField).join('; ')}.
            </p>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 flex-wrap mt-2">
        <button
          type="button"
          className="btn-primary btn-sm"
          disabled={Boolean(busy) || blocked}
          aria-describedby={blocked ? blockedId : undefined}
          onClick={onSave}
        >
          {busy === 'saving' ? 'Saving…' : 'Save setup to server'}
        </button>
        <button
          type="button"
          className="btn-secondary btn-sm"
          // Disabled only where there is provably nothing to load. An unknown
          // state is still offered: the probe may have failed for a reason
          // that has since gone away, and refusing the button would leave the
          // operator with no way to find out.
          disabled={Boolean(busy) || status.state === 'none'}
          onClick={onLoad}
        >
          {busy === 'loading' ? 'Loading…' : 'Load setup from server'}
        </button>
        <button
          type="button"
          className="btn-secondary btn-sm"
          disabled={Boolean(busy) || status.state === 'none'}
          onClick={onForget}
        >
          {busy === 'forgetting' ? 'Forgetting…' : 'Forget the saved setup'}
        </button>
        <span className="text-secondary text-xs">·</span>
        <button
          type="button"
          className="btn-secondary btn-sm"
          disabled={blocked}
          aria-describedby={blocked ? blockedId : undefined}
          onClick={onExportFile}
        >
          Save setup to file
        </button>
        <button type="button" className="btn-secondary btn-sm" onClick={onImportFile}>
          Load setup from file
        </button>
        <button
          type="button"
          className="btn-secondary btn-sm"
          onClick={onPaste}
          aria-expanded={pasteOpen}
        >
          {pasteOpen ? 'Cancel paste' : 'Paste setup'}
        </button>
        <span className="text-secondary text-xs">0 Travian requests</span>
      </div>
    </div>
  )
}

/** One blocker as the panel says it: the column heading, and where it is wrong.
 *
 * The RULE is deliberately left out, and so are `describeBlockers`' parentheses.
 * That refusal is about a plan that was not sent and it prints the bound in
 * full; this is about a document that would not be readable, and it only has to
 * say which cell to go to -- the cell itself carries the bound, under the box.
 * Repeating the whole sentence would put the same words on screen twice
 * whenever a plan has just been refused, which is the shape the Plan-stage
 * redesign was undoing four panels down. */
function describeBlockedField(blocker) {
  return blocker.villages.length
    ? `${blocker.field} at ${blocker.villages.join(', ')}`
    : blocker.field
}

function statusTone(state) {
  if (state === 'saved') return 'text-success'
  if (state === 'error') return 'text-warning'
  return 'text-secondary'
}

/** The four states, each said as the thing it actually is. */
function describeStatus(status) {
  if (status.state === 'checking') return 'Checking the server for a saved setup…'
  if (status.state === 'saved') {
    // A time, not "recently": the question an operator on a second origin asks
    // is whether the saved copy is older than what they have been typing here.
    const when = status.savedAt ? new Date(status.savedAt).toLocaleString() : 'an unknown time'
    return `A setup is saved on the server, last written ${when}. Loading it replaces what the file import would — village by village, profile by profile.`
  }
  if (status.state === 'none') {
    return 'Nothing is saved on the server for this account yet. Save what you have typed, or load a file first and then save it.'
  }
  if (status.state === 'error') {
    return `The server could not say whether a setup is saved: ${status.message}. The buttons still work — this is only the check.`
  }
  return 'Not checked yet — connect an account and this says whether a setup is saved.'
}
