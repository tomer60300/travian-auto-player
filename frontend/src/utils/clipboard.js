/** Copy text to the clipboard, including where the modern API does not exist.
 *
 * `navigator.clipboard` is secure-context only, and this app is deliberately
 * served over plain http on a LAN address and over Tailscale — which is exactly
 * where the operator reads it from a phone. There the async API is simply
 * undefined, so the deprecated textarea + execCommand path is not a nicety, it
 * is the only one that works.
 *
 * Returns whether the copy went through, so the caller can report honestly
 * instead of claiming success into a clipboard that received nothing.
 */
export async function copyToClipboard(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Permission denied, or the document was not focused. Fall through to the
      // fallback rather than report a failure we can still avoid.
    }
  }
  const area = document.createElement('textarea')
  area.value = text
  // Read-only and off-screen, but still selectable: `display: none` cannot be.
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.top = '-1000px'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  // iOS ignores select() on a textarea unless the range is set explicitly, and
  // a phone over Tailscale is the case this fallback exists for.
  area.setSelectionRange(0, text.length)
  let copied = false
  try {
    copied = document.execCommand('copy')
  } catch {
    copied = false
  }
  area.remove()
  return copied
}
