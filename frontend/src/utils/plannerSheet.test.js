import { describe, it, expect } from 'vitest'
import { ROUTE_SHEET_HEADER, routeSheetRow, routeSheetText } from './plannerSheet'

const ROW = {
  from: 'V10',
  to: 'V02',
  cargo: { lumber: 0, clay: 0, iron: 9323, crop: 1200 },
  cycleHours: 3,
  dispatch: '08:20',
  arrival: '09:38',
  merchants: 5,
}

describe('routeSheetRow', () => {
  it('is one tab-separated line with a cell per header column', () => {
    const line = routeSheetRow(ROW)
    expect(line).not.toContain('\n')
    expect(line.split('\t')).toHaveLength(ROUTE_SHEET_HEADER.split('\t').length)
  })

  it('splits cargo into the marketplace field order, zeros included', () => {
    // The four cargo numbers are the ones retyped into the game dialog, so they
    // have to be four cells in the order the dialog asks for them.
    expect(routeSheetRow(ROW).split('\t').slice(2, 6)).toEqual(['0', '0', '9323', '1200'])
  })

  it('writes numbers unformatted, so a paste survives a numeric field', () => {
    const line = routeSheetRow({ ...ROW, cargo: { ...ROW.cargo, crop: 1234567 } })
    expect(line).toContain('1234567')
    expect(line).not.toContain('1,234,567')
  })

  it('rounds fractional cargo and treats a missing resource as zero', () => {
    expect(routeSheetRow({ ...ROW, cargo: { iron: 10.6 } }).split('\t').slice(2, 6)).toEqual([
      '0',
      '0',
      '11',
      '0',
    ])
    expect(routeSheetRow({ ...ROW, cargo: undefined }).split('\t').slice(2, 6)).toEqual([
      '0',
      '0',
      '0',
      '0',
    ])
  })

  it('keeps the times verbatim — they are typed into the route dialog', () => {
    expect(routeSheetRow(ROW).split('\t').slice(7, 9)).toEqual(['08:20', '09:38'])
  })
})

describe('routeSheetText', () => {
  it('leads with the header and adds one line per row, with no trailing blank', () => {
    const text = routeSheetText([ROW, { ...ROW, from: 'V11' }])
    const lines = text.split('\n')
    expect(lines[0]).toBe(ROUTE_SHEET_HEADER)
    expect(lines).toHaveLength(3)
    expect(text.endsWith('\n')).toBe(false)
  })

  it('still yields a usable header for an empty plan', () => {
    expect(routeSheetText([])).toBe(ROUTE_SHEET_HEADER)
  })
})
