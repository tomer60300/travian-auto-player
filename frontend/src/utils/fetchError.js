/**
 * The server's own words for a failed read, as a string that is safe to render.
 *
 * Every page that surfaces a caught fetch error needs the same three things and
 * kept re-deriving them: FastAPI's `detail`, a sentence of our own when the
 * failure has no body at all (a network error, a CORS refusal, an aborted
 * request), and the guarantee that the result is a STRING -- `detail` is a LIST
 * for a Pydantic validation failure, and React throws
 * "Objects are not valid as a React child" on an array of objects.
 *
 * Named export, per the frontend convention for `src/utils/`.
 */
export function readErrorDetail(e, fallback) {
  const detail = e?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  const message = e?.response?.data?.message
  if (typeof message === 'string' && message.trim()) return message
  return fallback
}
