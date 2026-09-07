import { describe, expect, it } from 'vitest'
import { undoComplete, sweepComplete, liveSnapshotAllowed } from './plannerSafety'

describe('planner live-operation safety', () => {
  it('does not call undo complete with restoration outstanding', () => {
    expect(undoComplete({ clean: false, restore_state: { 1: ['route 10 -> enabled'] } })).toBe(false)
    expect(undoComplete({ clean: true, restore_state: { 1: ['route 10 -> enabled'] } })).toBe(false)
    expect(undoComplete({ clean: true })).toBe(true)
  })
  it('retains deferred creates and unresolved failures after all villages were visited', () => {
    expect(sweepComplete([], 3, [], false)).toBe(false)
    expect(sweepComplete([], 0, ['disable failed'], false)).toBe(false)
    expect(sweepComplete([], 0, [], true)).toBe(false)
    expect(sweepComplete([], 0, [], false)).toBe(true)
    expect(sweepComplete(null, 0, [], false)).toBe(false)
  })
  it('rechecks freshness at each chunk, not only the original click', () => {
    expect(liveSnapshotAllowed(1000, false, 1000 + 29 * 60000)).toBe(true)
    expect(liveSnapshotAllowed(1000, false, 1000 + 31 * 60000)).toBe(false)
    expect(liveSnapshotAllowed(null, false, 1000)).toBe(false)
    expect(liveSnapshotAllowed(null, true, 1000)).toBe(true)
  })
})
