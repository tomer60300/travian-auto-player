import { describe, expect, it } from 'vitest'

import {
  filenameFromDisposition,
  isDigestConflict,
  planDigestShort,
  yamlFilename,
  yamlResponseTransform,
} from './plannerExport'

const DIGEST = 'c'.repeat(64)

describe('filenameFromDisposition', () => {
  it('reads the quoted form the server sends', () => {
    expect(
      filenameFromDisposition(
        'attachment; filename="distribution-plan-abc123def456.yaml"',
        'fallback.yaml'
      )
    ).toBe('distribution-plan-abc123def456.yaml')
  })

  it('reads an unquoted name', () => {
    expect(filenameFromDisposition('attachment; filename=plan.yaml', 'fallback.yaml')).toBe(
      'plan.yaml'
    )
  })

  // RFC 6266 says the extended form wins where both are present.
  it('prefers the extended form, and decodes it', () => {
    expect(
      filenameFromDisposition(
        "attachment; filename=\"plain.yaml\"; filename*=UTF-8''pl%C3%A1n.yaml",
        'fallback.yaml'
      )
    ).toBe('plán.yaml')
  })

  it('falls back to the plain form when the extended one is malformed', () => {
    expect(
      filenameFromDisposition(
        "attachment; filename=\"plain.yaml\"; filename*=UTF-8''%E0%A4%A",
        'fallback.yaml'
      )
    ).toBe('plain.yaml')
  })

  it('falls back when there is no disposition at all', () => {
    expect(filenameFromDisposition(undefined, 'fallback.yaml')).toBe('fallback.yaml')
    expect(filenameFromDisposition('attachment', 'fallback.yaml')).toBe('fallback.yaml')
  })

  // The value reaches `a.download`, so a name carrying a separator is a
  // filename the operator did not choose.
  it('strips every directory component, on both separators', () => {
    expect(
      filenameFromDisposition('attachment; filename="../../etc/passwd"', 'fallback.yaml')
    ).toBe('passwd')
    expect(
      filenameFromDisposition('attachment; filename="C:\\\\Windows\\\\evil.yaml"', 'fallback.yaml')
    ).toBe('evil.yaml')
  })

  it('falls back when stripping leaves nothing usable', () => {
    expect(filenameFromDisposition('attachment; filename="../"', 'fallback.yaml')).toBe(
      'fallback.yaml'
    )
    expect(filenameFromDisposition('attachment; filename=".."', 'fallback.yaml')).toBe(
      'fallback.yaml'
    )
  })
})

describe('planDigestShort', () => {
  // The same twelve the server's own filename uses, so the figure on screen and
  // the figure in the file's name are the same string.
  it('is the first twelve hex characters', () => {
    expect(planDigestShort(DIGEST)).toBe('cccccccccccc')
  })

  it('says so when there is no digest', () => {
    expect(planDigestShort('')).toBe('—')
    expect(planDigestShort(undefined)).toBe('—')
  })
})

describe('yamlFilename', () => {
  it('names the file for the plan, not for the moment', () => {
    expect(yamlFilename(DIGEST)).toBe('distribution-plan-cccccccccccc.yaml')
  })

  it('has a name even with no digest', () => {
    expect(yamlFilename(null)).toBe('distribution-plan.yaml')
  })
})

describe('isDigestConflict', () => {
  it('is a 409 and nothing else', () => {
    expect(isDigestConflict({ response: { status: 409 } })).toBe(true)
    // 422 is a MALFORMED digest, refused as such so a mistyped token does not
    // send the operator re-reading a plan that never moved.
    expect(isDigestConflict({ response: { status: 422 } })).toBe(false)
    expect(isDigestConflict({ response: { status: 500 } })).toBe(false)
    expect(isDigestConflict({})).toBe(false)
    expect(isDigestConflict(undefined)).toBe(false)
  })
})

describe('yamlResponseTransform', () => {
  it('leaves a 200 body as the document it is', () => {
    expect(yamlResponseTransform('routes: []\n', {}, 200)).toBe('routes: []\n')
  })

  // A YAML document that happens to be valid JSON is still a document, so the
  // parse is driven by the status and never attempted on a success.
  it('does not parse a document that happens to be valid JSON', () => {
    expect(yamlResponseTransform('{}', {}, 200)).toBe('{}')
  })

  // The 409's whole point is that it names BOTH digests; leaving it unparsed
  // dropped that and left the panel showing its own fallback sentence.
  it('parses a refusal, so the detail survives', () => {
    const parsed = yamlResponseTransform('{"detail":"it moved from a to b"}', {}, 409)
    expect(parsed).toEqual({ detail: 'it moved from a to b' })
  })

  it('hands back an unparseable error body rather than throwing', () => {
    expect(yamlResponseTransform('<html>502</html>', {}, 502)).toBe('<html>502</html>')
  })
})
