export const SNAPSHOT_LIVE_TTL_MS = 30 * 60 * 1000

export function liveSnapshotAllowed(fetchedAt, allowStale, now = Date.now()) {
  return allowStale || (Number.isFinite(fetchedAt) && now - fetchedAt <= SNAPSHOT_LIVE_TTL_MS)
}

export function undoComplete(result) {
  return result.clean === true && !result.problems?.length &&
    !Object.values(result.must_delete_by_hand ?? {}).some((rows) => rows.length) &&
    !Object.values(result.restore_state ?? {}).some((rows) => rows.length)
}

export function sweepComplete(outstanding, createsLeft, problems, unsettled) {
  return Array.isArray(outstanding) && !outstanding.length && createsLeft === 0 &&
    !problems.length && !unsettled
}
