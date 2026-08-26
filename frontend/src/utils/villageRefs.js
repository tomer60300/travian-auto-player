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
