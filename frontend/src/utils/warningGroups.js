/**
 * Fold a flat warning list into readable groups.
 *
 * A whole-day preview emits several hundred lines, most of them one sentence
 * repeated per route or per store with only the numbers changed — the operator
 * pasted a 200-line wall and reasonably stopped reading, which wastes the two
 * lines that are unique. Grouping is by SHAPE: strip the digits and the
 * arrow-pair the sentence is about, and what remains is the sentence template.
 * One template = one group, shown once with a count, expandable to the full
 * lines for whoever needs the specifics.
 */

/** The sentence with its variable parts blanked: numbers, times, names between
 * quotes, and the "A -> B" pair a route warning leads with. */
export function warningSignature(warning) {
  return warning
    .replace(/^(Day|Night|[^:]{1,24}):\s*/, '$1: ') // keep the profile prefix
    .replace(/route [^:]+?(?= repeats| has)/, 'route ¤')
    .replace(/^([^:]*:\s*)[\w '&()|-]+?(?=:)/, '$1¤') // leading village name
    .replace(/[\d,.]+x/g, '¤x')
    .replace(/[\d][\d,.:]*/g, '¤')
}

/**
 * → `[{ signature, count, lines }]`, largest group first, original order kept
 * inside each group. Groups of one keep their full sentence as the signature.
 */
export function groupWarnings(warnings) {
  const groups = new Map()
  for (const line of warnings ?? []) {
    const sig = warningSignature(line)
    if (!groups.has(sig)) groups.set(sig, { signature: sig, count: 0, lines: [] })
    const group = groups.get(sig)
    group.count += 1
    group.lines.push(line)
  }
  return [...groups.values()].sort((a, b) => b.count - a.count)
}
