/** Section 10's output order, on the client side of it.
 *
 * *"Output order: readable plan first → operator confirms → then generate
 * YAML/code."* The server enforces that with a digest: `/plan` returns
 * `plan_digest` over the response it showed, `/plan/yaml` demands it back, and
 * a mismatch is a **409 naming both digests** with no document rendered. So the
 * confirmation step is not a dialog -- it is the digest, and this module is
 * what turns the server's answer into a file or into an honest refusal.
 *
 * Nothing here retries. A 409 means the plan the operator READ no longer
 * exists, and re-planning silently to make the download succeed would hand
 * them an authoritative-looking file describing a plan they never saw, which is
 * the exact outcome the digest exists to prevent.
 */

/** The filename the server asked for, or a fallback.
 *
 * `Content-Disposition` is respected rather than reconstructed because the
 * server names the file for the PLAN (`distribution-plan-<digest12>.yaml`) and
 * not for the moment -- so two downloads of one plan are one file, and a diff
 * between two files is a diff between two plans. Rebuilding the name here would
 * be a second implementation of that convention, free to drift.
 *
 * Every directory component is stripped, and that is a real check rather than
 * defensive noise: the value reaches `a.download`, so a name carrying `..` or a
 * separator is a filename the operator did not choose. Both RFC 6266 forms are
 * read -- the plain `filename=` and the extended `filename*=UTF-8''...` -- with
 * the extended one preferred, as the RFC requires.
 */
export function filenameFromDisposition(header, fallback) {
  const text = String(header ?? '')
  const extended = /filename\*\s*=\s*[^']*'[^']*'([^;]+)/i.exec(text)
  const plain = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/i.exec(text)
  let raw = null
  if (extended) {
    try {
      raw = decodeURIComponent(extended[1].trim())
    } catch {
      // A malformed percent-escape is not a filename. Fall through to the
      // plain form rather than handing the browser the raw bytes.
      raw = null
    }
  }
  if (raw == null && plain) raw = (plain[1] ?? plain[2] ?? '').trim()
  if (!raw) return fallback
  // Windows separators too: this app is served over Tailscale to a phone and
  // read on a Windows desktop, and only one of those treats `\` as a path.
  const base = raw.split(/[/\\]/).pop()
  if (!base || base === '.' || base === '..') return fallback
  return base
}

/** A digest, short enough to read and long enough to be an identity.
 *
 * Twelve hex characters, which is what the server's own filename uses -- so the
 * figure on screen and the figure in the downloaded file's name are the same
 * string, and an operator comparing them is comparing like with like.
 */
export function planDigestShort(digest) {
  const text = String(digest ?? '')
  return text ? text.slice(0, 12) : '—'
}

/** The fallback filename, used only when the server sent no disposition. */
export function yamlFilename(digest) {
  const short = String(digest ?? '').slice(0, 12)
  return short ? `distribution-plan-${short}.yaml` : 'distribution-plan.yaml'
}

/** Keep a successful YAML body as raw text, and parse a refusal as JSON.
 *
 * `/plan/yaml` answers a 200 with a YAML document and a 409 with FastAPI's
 * ordinary `{"detail": ...}` JSON, so one response type does not cover both --
 * and getting this wrong is not cosmetic. Replacing axios's response transform
 * outright left `error.response.data` as the unparsed JSON string, `detail`
 * came back undefined, and the panel fell through to its own fallback sentence:
 * the 409's whole point -- it NAMES BOTH DIGESTS -- was silently dropped, so
 * the operator was told the plan had moved and not what it moved from or to.
 *
 * Status-driven rather than try-parse-everything, because a YAML document that
 * happens to be valid JSON is still a document: `{}` is both, and a plan file
 * must never arrive as an object.
 */
export function yamlResponseTransform(body, _headers, status) {
  if (status >= 200 && status < 300) return body
  try {
    return JSON.parse(body)
  } catch {
    // Not JSON either. Handing back the raw text lets `errorDetail` fall
    // through to its fallback, which is the honest outcome.
    return body
  }
}

/** Did this response refuse because the plan moved?
 *
 * 409 and only 409. A 422 is a malformed digest -- refused as such deliberately,
 * so a mistyped token does not send the operator re-reading a plan that never
 * moved -- and every other status is an ordinary failure.
 */
export function isDigestConflict(error) {
  return error?.response?.status === 409
}
