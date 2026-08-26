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
