/** The setup sheet's clipboard format.
 *
 * Live route creation is opt-in and off by default, so this sheet is the
 * working output path: its rows are retyped into the game's trade-route dialog,
 * or pasted into the spreadsheet this planner replaces. Tab-separated serves
 * both — a paste lands one value per cell with no import dialog — and the
 * numbers are written UNFORMATTED, because the thousands separators the table
 * displays are exactly what a numeric game field and a non-English spreadsheet
 * locale reject.
 *
 * Cargo becomes one column per resource rather than the table's single "cargo
 * per send" cell: those four numbers are the ones retyped by hand, so they have
 * to arrive as four cells.
 */

/** The marketplace's field order — the order the numbers are typed in-game. */
const CARGO_ORDER = ['lumber', 'clay', 'iron', 'crop']

export const ROUTE_SHEET_HEADER = [
  'From',
  'To',
  'Lumber',
  'Clay',
  'Iron',
  'Crop',
  'Cycle (h)',
  'Send at',
  'Arrives',
  'Merchants',
].join('\t')

/** One sheet row as a tab-separated line. Names are resolved by the caller, so
 *  the clipboard can never disagree with the village names on screen. */
export function routeSheetRow(row) {
  return [
    row.from,
    row.to,
    ...CARGO_ORDER.map((resource) => Math.round(Number(row.cargo?.[resource]) || 0)),
    row.cycleHours,
    row.dispatch,
    row.arrival,
    row.merchants,
  ].join('\t')
}

/** The whole sheet, header included. No trailing newline: it would paste as an
 *  extra empty row. */
export function routeSheetText(rows) {
  return [ROUTE_SHEET_HEADER, ...rows.map((row) => routeSheetRow(row))].join('\n')
}
