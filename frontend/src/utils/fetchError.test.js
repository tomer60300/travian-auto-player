import { describe, it, expect } from 'vitest'

import { readErrorDetail } from './fetchError'

const FALLBACK = 'Failed to load'

describe('readErrorDetail', () => {
  it('prefers the server detail', () => {
    expect(readErrorDetail({ response: { data: { detail: 'Travian returned 503' } } }, FALLBACK))
      .toBe('Travian returned 503')
  })

  it('falls back to message when there is no detail', () => {
    expect(readErrorDetail({ response: { data: { message: 'gateway timeout' } } }, FALLBACK))
      .toBe('gateway timeout')
  })

  it('uses the caller sentence for a failure with no body at all', () => {
    expect(readErrorDetail(new Error('Network Error'), FALLBACK)).toBe(FALLBACK)
    expect(readErrorDetail(undefined, FALLBACK)).toBe(FALLBACK)
    expect(readErrorDetail({ response: {} }, FALLBACK)).toBe(FALLBACK)
  })

  // The whole reason this is not an inline `?.detail`: a 422 answers with a
  // LIST of validation errors, and React refuses to render an array of
  // objects -- it throws, taking the page down instead of showing the error.
  it('never returns a non-string detail', () => {
    const validation = { response: { data: { detail: [{ loc: ['body', 'x'], msg: 'field required' }] } } }
    expect(readErrorDetail(validation, FALLBACK)).toBe(FALLBACK)
    expect(readErrorDetail({ response: { data: { detail: { code: 7 } } } }, FALLBACK)).toBe(FALLBACK)
  })

  // An empty string is not an explanation.
  it('ignores a blank detail', () => {
    expect(readErrorDetail({ response: { data: { detail: '   ' } } }, FALLBACK)).toBe(FALLBACK)
  })
})
