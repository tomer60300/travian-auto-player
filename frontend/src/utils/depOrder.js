/**
 * Find use-before-declaration in React dependency arrays.
 *
 * React evaluates a hook's dependency array AT RENDER, so naming a `const`
 * declared further down the component throws a temporal-dead-zone
 * ReferenceError and takes the whole page down — this crashed the Resource
 * Planner twice in one day ("Cannot access 'ln'/'un' before initialization"),
 * and both times the ad-hoc audit missed it because prettier formats long
 * arrays with `},` and `[` on separate lines.
 *
 * This is a deliberately dumb text scan, not a parser: it only has to be
 * right about the two shapes prettier actually emits for dependency arrays
 * (`}, [a, b])` inline and `},\n [\n a,\n b\n ])` split), and a dumb scan
 * cannot silently skip what a clever regex fails to match.
 */

/** Every `(lineNumber, identifier, declaredAtLine)` where a dependency array
 * names a `const` declared on a LATER line. Empty means the file cannot crash
 * this way. */
export function forwardRefsInDepArrays(source) {
  const lines = source.split('\n')

  const declaredAt = new Map()
  for (let i = 0; i < lines.length; i += 1) {
    const m = lines[i].match(/^\s*const\s+\[?\s*(\w+)/)
    if (m && !declaredAt.has(m[1])) declaredAt.set(m[1], i + 1)
  }

  const offenders = []
  for (let i = 0; i < lines.length; i += 1) {
    // A dependency array starts where a hook callback closes: `}, [` — either
    // on one line or with the bracket alone on the next.
    let j = -1
    if (/\},\s*\[/.test(lines[i])) j = i
    else if (/^\s*\},\s*$/.test(lines[i]) && /^\s*\[\s*$/.test(lines[i + 1] ?? '')) j = i + 1
    if (j === -1) continue

    // Collect until the closing `])`, spanning lines.
    let buf = lines[j].slice(lines[j].indexOf('[') + 1)
    let k = j
    while (!buf.includes(']') && k < lines.length - 1) {
      k += 1
      buf += ` ${lines[k]}`
    }
    buf = buf.slice(0, buf.indexOf(']'))

    for (const ident of buf.match(/[A-Za-z_$][\w$]*/g) ?? []) {
      const decl = declaredAt.get(ident)
      if (decl !== undefined && decl > i + 1) {
        offenders.push({ line: i + 1, identifier: ident, declaredAt: decl })
      }
    }
  }
  return offenders
}
