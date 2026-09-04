/** Turning what the operator typed into village ids, and back again.
 *
 * The operator knows their villages as "02" and "18". A village id is an internal
 * handle, and `allocation.village_label` on the backend says outright that nobody
 * running the account knows which village 53629 is — so any field that reaches a
 * person should speak names, and only the payload should speak ids.
 *
 * The dangerous failure is silent. Typing "2" where "02" was meant, and having it
 * quietly dropped, leaves the operator believing a village is excluded while the
 * next run happily draws on it. So unresolved entries come back alongside the ids
 * rather than being filtered away.
 */

/** Resolve comma-separated village names (or ids) against a village list. */
export function resolveVillageNames(text, villages) {
  const byName = new Map()
  for (const village of villages ?? []) {
    if (village.name) byName.set(String(village.name).trim().toLowerCase(), village.village_id)
    // Ids are accepted too: a file, a log line or a warning may hand one over,
    // and refusing it would make the operator translate it themselves.
    byName.set(String(village.village_id), village.village_id)
  }
  const ids = []
  const unknown = []
  for (const raw of String(text ?? '').split(',')) {
    const entry = raw.trim()
    if (!entry) continue
    const found = byName.get(entry.toLowerCase())
    if (found == null) unknown.push(entry)
    else if (!ids.includes(found)) ids.push(found)
  }
  return { ids, unknown }
}

/** The reverse, for showing what a loaded file or an earlier session stored. */
export function namesForVillageIds(ids, villages) {
  const byId = new Map(
    (villages ?? []).map((v) => [v.village_id, v.name || String(v.village_id)])
  )
  return (ids ?? []).map((id) => byId.get(id) ?? String(id)).join(', ')
}

/** Entries in the "Never disable" list that will protect nothing, and why.
 *
 * `protect_destinations` is a list of STRINGS on the wire, each a village id
 * ("53629") or coordinates ("46|133") -- coordinates because a hand-made route
 * to a foreign target has no usable village id. The server validates the SHAPE
 * and nothing else, which it has to: it cannot know whether an id exists in an
 * account it was not sent. So `4688` passes, `int("4688")` is added to the
 * protected set, no live route matches it, and the run switches off the very
 * route the operator was protecting. The backend's own field validator names
 * this case -- *"A typo ('4688' for '46|88') that is silently ignored leaves the
 * operator believing a route is protected when it is not, and the very next run
 * switches it off"* -- and can only reject the shapes, not the misses.
 *
 * The page holds the village list, so the page is where a miss is knowable.
 *
 * Two kinds of miss, and only these two are reported:
 *
 *   * a BARE entry (no `|`) that is not one of the account's village ids. Note
 *     `Number(entry)`, not a name lookup: this account names its villages "02"
 *     and "18", so a name typed here IS a bare integer -- `int("02")` is 2, the
 *     server protects village 2, and the route keeps being disabled. A name is
 *     therefore a miss, and the id is offered back.
 *   * an entry that is neither. The server 422s it; saying so here is faster.
 *
 * COORDINATES ARE NEVER FLAGGED, even when no village of this account sits
 * there. That is the normal case rather than an error: the routes worth
 * protecting are the hand-made ones to targets the account does not own.
 *
 * A suggestion is offered only where it can be grounded in the village list or
 * in the shape itself, and never invented:
 *
 *   * the entry is some village's NAME -> that village's id (the higher
 *     confidence of the two, so it wins);
 *   * four or six digits -> the middle split as coordinates, since a Travian
 *     coordinate is one to three digits. Two digits are deliberately NOT split:
 *     "99" as "9|9" is noise, and five digits do not split evenly at all --
 *     which is the shape a real village id has.
 */
export function unresolvedProtectedEntries(text, villages) {
  const ids = new Set((villages ?? []).map((v) => v.village_id))
  const byName = new Map()
  for (const village of villages ?? []) {
    if (village.name) byName.set(String(village.name).trim().toLowerCase(), village.village_id)
  }
  const problems = []
  for (const raw of String(text ?? '').split(',')) {
    const entry = raw.trim()
    if (!entry) continue
    // Shape-valid coordinates say nothing this function can check.
    if (entry.includes('|')) continue
    if (/^\d+$/.test(entry) && ids.has(Number(entry))) continue
    const named = byName.get(entry.toLowerCase())
    if (named != null) {
      problems.push({ entry, suggestion: String(named) })
      continue
    }
    const split =
      /^\d+$/.test(entry) && (entry.length === 4 || entry.length === 6)
        ? `${entry.slice(0, entry.length / 2)}|${entry.slice(entry.length / 2)}`
        : null
    problems.push({ entry, suggestion: split })
  }
  return problems
}

/** The ids to exclude for one target: what was typed, or what was stored.
 *
 * A loaded setup file carries `exclude_origins` as ids and no typed text at all,
 * so reading only the text drops the exclusion silently -- which is worse than
 * never having had it, because the operator has every reason to believe it is
 * still in force. Ids are the stored form because they are stable across renames;
 * the text exists only so the input can hold a half-finished name.
 *
 * The typed text wins when it EXISTS, even empty, because clearing the field is a
 * deliberate act and must not fall back to what the file said.
 */
export function excludedOriginIds(target, villages) {
  if (target?.exclude_origins_text != null) {
    return resolveVillageNames(target.exclude_origins_text, villages).ids
  }
  return (target?.exclude_origins ?? []).filter((id) => Number.isInteger(id) && id > 0)
}
