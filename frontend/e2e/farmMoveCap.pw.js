/**
 * FarmLists "move targets between lists" must not destroy the targets whose
 * add to the destination the server refused.
 *
 * `handleTransfer` (`src/pages/FarmLists.jsx`) adds each selected slot to the
 * destination list, then -- for "move" -- deletes it from the source. The
 * delete step used to iterate every SELECTED slot, not the ones whose add
 * actually succeeded, so a destination that filled up partway through (the
 * game caps a farm list at 100 slots) had its failed adds deleted from the
 * source anyway: the target existed in neither list, with the toast still
 * reading "Moved N target(s)" in green.
 *
 * Driven here: 10 selected slots, a destination that accepts exactly 7 adds
 * (201) and then refuses the rest with the same 502 `{"detail": ...}` shape
 * `POST /farm/lists/{id}/targets` returns for a real refusal
 * (`src/travian_api/web/routes/farm.py`, `add_target`). The three refused
 * slots must stay in the source, the toast must be a warning naming both
 * counts and the refusal reason, and delete must never be called for them.
 *
 * NO BACKEND AND NO GAME REQUEST, the same two fail-closed mechanisms
 * `accessibleNames.pw.js` documents: `page.route('**\/api/**')` answers only
 * the calls this flow makes (with a small in-memory model of the source
 * list's slots so the UI's own refresh reflects what the mock actually
 * deleted) and `route.abort` closes everything else; the one WS this page can
 * open (loop mode) is never started. There is a live Travian account on this
 * machine.
 */

import { expect, test } from '@playwright/test'

const PLAYER = 'e2e-operator'
const SERVER = 'https://ts2.x1.europe.travian.com'
const CAPITAL = 20002

const SOURCE_ID = 1
const DEST_ID = 2
const DEST_ACCEPTS = 7 // the destination confirms the first 7 adds, then refuses
const REFUSAL_REASON = 'errorRaidListSlotLimit'

function slotFor(id) {
  const n = id - 500
  return {
    id, x: n, y: 0, name: `Target ${n}`,
    population: 100, distance: 1, is_active: true,
    troops: {}, total_booty: 0, total_raids: 0, last_raid: null,
  }
}

test('a move leaves the refused targets in the source and warns, instead of deleting them', async ({ page }) => {
  const remainingSourceIds = new Set(Array.from({ length: 10 }, (_, i) => 501 + i))
  const deletedIds = []
  let addAttempts = 0

  await page.routeWebSocket(/.*/, (ws) => ws.close())

  await page.route('**/api/**', (route) => {
    const req = route.request()
    const method = req.method()
    const path = new URL(req.url()).pathname

    if (path.endsWith('/users/me')) {
      return route.fulfill({ json: { id: 1, username: PLAYER, is_active: true } })
    }
    if (path.endsWith('/travian/status')) {
      return route.fulfill({
        json: {
          connected: true,
          server_url: SERVER,
          player_name: PLAYER,
          tribe_id: 1,
          active_village_id: CAPITAL,
          villages: [{ id: CAPITAL, name: '02', x: 0, y: 0 }],
        },
      })
    }
    if (method === 'GET' && path.endsWith('/farm/lists')) {
      return route.fulfill({
        json: [
          {
            id: SOURCE_ID, name: 'Source',
            slots_amount: remainingSourceIds.size, active_slots: remainingSourceIds.size,
            total_booty: 0,
          },
          { id: DEST_ID, name: 'Dest', slots_amount: DEST_ACCEPTS, active_slots: DEST_ACCEPTS, total_booty: 0 },
        ],
      })
    }
    if (method === 'GET' && path.endsWith(`/farm/lists/${SOURCE_ID}`)) {
      const slots = [...remainingSourceIds].sort((a, b) => a - b).map(slotFor)
      return route.fulfill({ json: { id: SOURCE_ID, name: 'Source', slots } })
    }
    const addMatch = path.match(/\/farm\/lists\/(\d+)\/targets$/)
    if (method === 'POST' && addMatch && Number(addMatch[1]) === DEST_ID) {
      addAttempts++
      if (addAttempts <= DEST_ACCEPTS) {
        return route.fulfill({ status: 201, json: { list_id: DEST_ID, x: 0, y: 0 } })
      }
      return route.fulfill({ status: 502, json: { detail: REFUSAL_REASON } })
    }
    const delMatch = path.match(/\/farm\/lists\/(\d+)\/targets\/(\d+)$/)
    if (method === 'DELETE' && delMatch && Number(delMatch[1]) === SOURCE_ID) {
      const slotId = Number(delMatch[2])
      remainingSourceIds.delete(slotId)
      deletedIds.push(slotId)
      return route.fulfill({ status: 204 })
    }
    return route.abort('blockedbyclient')
  })

  await page.addInitScript(() => localStorage.setItem('token', 'e2e-not-a-real-token'))

  await page.goto('/farm')
  await page.getByRole('cell', { name: 'Source', exact: true }).click()
  await expect(page.getByText('Target 1', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: /^Select All/ }).click()
  await expect(page.getByText('10 selected')).toBeVisible()

  const destinationSelect = page.locator('select').filter({ has: page.locator('option', { hasText: '-- Destination --' }) })
  await destinationSelect.selectOption({ label: 'Dest' })
  const modeSelect = page.locator('select').filter({ has: page.locator('option', { hasText: 'Move' }) })
  await modeSelect.selectOption('move')

  await page.getByRole('button', { name: 'Move 10' }).click()

  // The refusal reason travels through, and the toast is a warning, not a
  // celebration, whenever any add failed.
  await expect(page.locator('.toast-warning')).toContainText(
    `7 moved; 3 left in "Source" because "Dest" refused them: ${REFUSAL_REASON}`
  )
  await expect(page.locator('.toast-success')).toHaveCount(0)

  // Only the 7 slots whose add the destination confirmed were ever deleted --
  // the 3 it refused (508, 509, 510) must never reach the DELETE endpoint.
  expect(deletedIds.sort((a, b) => a - b)).toEqual([501, 502, 503, 504, 505, 506, 507])

  // The source, re-fetched after the transfer, still holds the 3 refused
  // targets -- not zero of them, which is what "delete everything selected"
  // produces.
  await expect(page.getByText('Target 8')).toBeVisible()
  await expect(page.getByText('Target 9')).toBeVisible()
  await expect(page.getByText('Target 10')).toBeVisible()
  await expect(page.getByText('Target 1', { exact: true })).toHaveCount(0)
})
