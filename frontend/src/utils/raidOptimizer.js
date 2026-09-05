// Raid Composition Optimizer — pure math, no DOM/React.
//
// Designs raiding forces for Teuton inventories under two ceilings:
//   1. Zero-Cas DEF — highest DEF where the comp takes zero losses.
//   2. Budget DEF + resource budget — losses at that DEF cost ≤ budget.
//
// Returns four strategies (BALANCE / RAIDS / ZERO-CAS / CHEAPEST LOSS),
// each with a top pick plus a ranked alternatives list. Smithy levels
// per unit type adjust attack via the standard Travian formula:
//   improved_atk = BASE + (BASE + 300 × UPKEEP / 7) × (1.007^L − 1)

export const K = 1.5

// Order matters for the deterministic loops. The .short label is what
// renders in the composition pills (e.g. "5C + 11A + 16T").
export const UNITS = [
  { key: 'c',  short: 'C',  name: 'Clubswinger',     plural: 'Clubswingers',     atk:  40, upk: 1, cost:  250, carry: 60,  troopId: 't1' },
  { key: 'sp', short: 'Sp', name: 'Spearman',        plural: 'Spearmen',         atk:  10, upk: 1, cost:  340, carry: 40,  troopId: 't2' },
  { key: 'a',  short: 'A',  name: 'Axeman',          plural: 'Axemen',           atk:  60, upk: 1, cost:  490, carry: 50,  troopId: 't3' },
  { key: 'pa', short: 'Pa', name: 'Paladin',         plural: 'Paladins',         atk:  55, upk: 2, cost: 1005, carry: 110, troopId: 't5' },
  { key: 't',  short: 'T',  name: 'Teutonic Knight', plural: 'Teutonic Knights', atk: 150, upk: 3, cost: 1525, carry: 80,  troopId: 't6' },
]

export const UNIT_BY_KEY = Object.fromEntries(UNITS.map((u) => [u.key, u]))

// Lone-attacker rule: any single unit with base ATK < 83 dies even
// against an empty target. Smithy L20 never lifts a Teuton unit above
// 83, so this set is stable regardless of forge level.
const SOLO_DOOMED = new Set(UNITS.filter((u) => u.atk < 83).map((u) => u.key))

// ── Smithy ────────────────────────────────────────────────────────

export function smithyAtk(unit, level) {
  if (!level || level <= 0) return unit.atk
  return unit.atk + (unit.atk + 300 * unit.upk / 7) * (Math.pow(1.007, level) - 1)
}

// Build a 5-tuple of effective ATKs in UNITS order.
export function buildAtks(smithyLv) {
  return UNITS.map((u) => smithyAtk(u, smithyLv?.[u.key] || 0))
}

// ── Combat math ───────────────────────────────────────────────────

export function offOf(p, atks) {
  if (atks) return p.c * atks[0] + p.sp * atks[1] + p.a * atks[2] + p.pa * atks[3] + p.t * atks[4]
  return p.c * 40 + p.sp * 10 + p.a * 60 + p.pa * 55 + p.t * 150
}

export function totalUnits(p) {
  return p.c + p.sp + p.a + p.pa + p.t
}

export function deployCost(p) {
  return p.c * 250 + p.sp * 340 + p.a * 490 + p.pa * 1005 + p.t * 1525
}

export function carryOf(p) {
  return p.c * 60 + p.sp * 40 + p.a * 50 + p.pa * 110 + p.t * 80
}

export function lossFraction(off, def) {
  if (off <= def) return 1.0
  const r = def / off
  // K = 1.5 ⇒ r^K = r·√r ; ~3× faster than Math.pow.
  const x = r * Math.sqrt(r)
  return x / (1 + x)
}

export function isWipe(p, def, atks) {
  if (totalUnits(p) === 1) {
    for (const u of UNITS) {
      if (p[u.key] === 1 && SOLO_DOOMED.has(u.key)) return true
    }
  }
  return offOf(p, atks) <= def
}

export function casualties(p, def, atks) {
  const off = offOf(p, atks)
  const n = totalUnits(p)
  const dead = { c: 0, sp: 0, a: 0, pa: 0, t: 0 }

  if (n === 1) {
    for (const u of UNITS) {
      if (p[u.key] === 1) {
        if (SOLO_DOOMED.has(u.key)) { dead[u.key] = 1; return dead }
        dead[u.key] = off > def ? 0 : 1
        return dead
      }
    }
  }
  if (off <= def) {
    for (const u of UNITS) dead[u.key] = p[u.key]
    return dead
  }
  const loss = lossFraction(off, def)
  for (const u of UNITS) dead[u.key] = Math.round(p[u.key] * loss)
  return dead
}

export function casualtyResourceCost(p, def, atks) {
  const dead = casualties(p, def, atks)
  let cost = 0
  for (const u of UNITS) cost += dead[u.key] * u.cost
  return cost
}

export function totalDead(dead) {
  return dead.c + dead.sp + dead.a + dead.pa + dead.t
}

// Highest DEF where total casualty count ≤ maxCas AND the raid isn't a wipe.
export function maxDefForCasualties(p, maxCas, atks) {
  let lo = 1, hi = 2000, best = 0
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (isWipe(p, mid, atks)) { hi = mid - 1; continue }
    if (totalDead(casualties(p, mid, atks)) <= maxCas) { best = mid; lo = mid + 1 }
    else hi = mid - 1
  }
  return best
}

// Highest DEF where casualty resource cost ≤ budget AND not a wipe.
export function maxDefForBudget(p, budget, atks) {
  let lo = 1, hi = 2000, best = 0
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (isWipe(p, mid, atks)) { hi = mid - 1; continue }
    if (casualtyResourceCost(p, mid, atks) <= budget) { best = mid; lo = mid + 1 }
    else hi = mid - 1
  }
  return best
}

// Walk DEF upward and emit every breakpoint where the casualty profile
// changes. Stops one breakpoint past the cost budget so the user can
// see what blows them over the threshold.
export function lossPath(p, budget, atks) {
  const limit = Math.max(maxDefForBudget(p, budget, atks) + 50, 1)
  const points = []
  let prevSig = ''
  let lastDef = 0
  const STEP = 5

  for (let def = 1; def <= limit; def += STEP) {
    if (isWipe(p, def, atks)) break
    const dead = casualties(p, def, atks)
    const sig = `${dead.c},${dead.sp},${dead.a},${dead.pa},${dead.t}`
    if (sig !== prevSig) {
      // Binary-refine: exact DEF where this casualty signature first appears.
      let lo = lastDef + 1, hi = def, first = def
      while (lo <= hi) {
        const mid = (lo + hi) >> 1
        if (isWipe(p, mid, atks)) { hi = mid - 1; continue }
        const dMid = casualties(p, mid, atks)
        const sigMid = `${dMid.c},${dMid.sp},${dMid.a},${dMid.pa},${dMid.t}`
        if (sigMid === sig) { first = mid; hi = mid - 1 }
        else lo = mid + 1
      }
      const deadAtFirst = casualties(p, first, atks)
      let cost = 0
      for (const u of UNITS) cost += deadAtFirst[u.key] * u.cost
      points.push({ def: first, dead: deadAtFirst, cost, exceedsBudget: cost > budget })
      prevSig = sig
      if (cost > budget) break
    }
    lastDef = def
  }
  return points
}

// ── Search ────────────────────────────────────────────────────────

const EMPTY_RESULT = { balance: null, raids: null, zero: null, death: null }

// Per-type search bound for the EXHAUSTIVE small-packet search (phase 1).
//
// This is a PERFORMANCE bound, not a solution bound. The 5-nested-loop phase
// explodes with large inventories without a cap. Packets that exceed the cap
// here are covered by phase 2 (R-based bottleneck-tight enumeration), so no
// Pareto-optimal solution is lost.
//
// Cap is a MONOTONIC function of the unit's own inventory count — adding
// troops of type X can only expand X's cap, never shrink it. This avoids
// the ratio-based instability of the previous design where adding clubs
// could shrink the TK cap and silently lose a TK-heavy front.
function capRange(unit, invK) {
  if (invK === 0) return 0

  // Spears/paladins have low offensive value — caps stay tight so the
  // inner loop doesn't waste cycles on dominated shapes.
  if (unit.key === 'sp') return Math.min(invK, 20)
  if (unit.key === 'pa') return Math.min(invK, 40)

  // Primary offensive units: stockpile-tier ceilings. The mid-band only
  // has to cover "balanced" packets — phase 2 handles bottleneck-tight
  // large packets without needing a wider cap here.
  if (unit.atk >= 100) {
    return Math.min(invK, invK >= 200 ? 130 : 50)
  }
  return Math.min(invK, invK >= 150 ? 150 : 100)
}

// Phase 1's cost is the PRODUCT of its five per-unit bounds. capRange() alone
// still lets that product reach the billions for a real, all-five-slots-full
// Teuton stockpile (1500/200/800/60/400 -> 150/20/150/40/130 -> ~2.5B), which
// locks up whatever thread runs it for tens of seconds to minutes. Phase 2
// (R-based bottleneck-tight enumeration, below) already covers every
// Pareto-optimal packet phase 1's caps can't reach, so shrinking phase 1
// further than capRange() costs no correctness -- only exhaustiveness in the
// mid-band it exists to cover, and only once the naive product is already
// too large to be a "mid-band, small-packet" search in any useful sense.
const PHASE1_BUDGET = 1_000_000

// Scale five independent per-unit caps down together (proportionally, in
// log-space) so the product of (cap+1) fits `budget`, without any cap ever
// exceeding its own raw value. A no-op whenever the raw product already fits
// -- which covers every case capRange() was tuned against, including the
// small early-game army this file's own comment measures at ~600k.
function fitCapsToBudget(rawCaps, budget) {
  const rawProduct = rawCaps.reduce((p, c) => p * (c + 1), 1)
  if (rawProduct <= budget) return rawCaps

  const scale = Math.pow(budget / rawProduct, 1 / rawCaps.length)
  const caps = rawCaps.map((c) => Math.min(c, Math.max(0, Math.round((c + 1) * scale) - 1)))

  // Rounding can still leave the product a hair over budget -- trim the
  // currently-largest cap down by one at a time until it fits.
  let product = caps.reduce((p, c) => p * (c + 1), 1)
  while (product > budget) {
    let idx = 0
    for (let i = 1; i < caps.length; i++) if (caps[i] > caps[idx]) idx = i
    if (caps[idx] <= 0) break
    product = (product / (caps[idx] + 1)) * caps[idx]
    caps[idx] -= 1
  }
  return caps
}

// Composition id string used for rank lookups.
function compId(v) {
  return UNITS.map((u) => v.p[u.key]).join(',')
}

// Map composition id → normalized rank in [0, 1]. Ties share rank.
function normalizedRanks(sortedList, scoreFn) {
  const m = new Map()
  const N = sortedList.length
  let prevScore = Symbol()
  let rank = 0
  for (let i = 0; i < N; i++) {
    const v = sortedList[i]
    const s = scoreFn(v)
    if (s !== prevScore) { rank = i + 1; prevScore = s }
    m.set(compId(v), N > 1 ? (rank - 1) / (N - 1) : 0)
  }
  return m
}

function scoreStrategies(unique) {
  const byRaids = [...unique].sort((x, y) =>
    y.raids - x.raids
    || x.costAtBudget - y.costAtBudget
    || x.cost - y.cost
    || y.maxZero - x.maxZero
  )
  const byZero = [...unique].sort((x, y) =>
    y.maxZero - x.maxZero
    || x.cost - y.cost
    || y.raids - x.raids
  )
  const byDeath = [...unique].sort((x, y) =>
    x.costAtBudget - y.costAtBudget
    || x.cost - y.cost
    || y.raids - x.raids
  )

  // BALANCE: weighted composite of normalized ranks across the three axes.
  const rR = normalizedRanks(byRaids, (v) => -v.raids)
  const rZ = normalizedRanks(byZero,  (v) => -v.maxZero)
  const rD = normalizedRanks(byDeath, (v) => v.costAtBudget)
  const scored = unique.map((v) => ({
    ...v,
    balanceScore: 0.4 * rR.get(compId(v)) + 0.4 * rD.get(compId(v)) + 0.2 * rZ.get(compId(v)),
  }))
  const byBalance = scored.sort((a, b) =>
    a.balanceScore - b.balanceScore
    || b.raids - a.raids
    || a.cost - b.cost
  )

  return {
    raids:   { top: byRaids[0],   alts: byRaids.slice(1, 4) },
    zero:    { top: byZero[0],    alts: byZero.slice(1, 3) },
    death:   { top: byDeath[0],   alts: byDeath.slice(1, 3) },
    balance: { top: byBalance[0], alts: byBalance.slice(1, 4) },
  }
}

// Main entry: find candidate compositions and rank by 4 strategies.
//
// Two-phase search over the Pareto frontier:
//
//   PHASE 1 — Exhaustive small-packet sweep. Five nested loops bounded by
//             capRange(). Only runs when defZero > 0 because that constraint
//             can force packets BELOW the inventory-bottleneck (rounding-up
//             to 1 casualty kills the candidate). When defZero == 0 every
//             bottleneck-tight packet trivially satisfies it, so phase 2
//             dominates all R-axis fronts and phase 1 is skipped entirely.
//
//   PHASE 2 — R-based bottleneck-tight enumeration. For each non-empty
//             unit subset (mask 1..31) and each raid count R, build the
//             packet pack_k = floor(inv_k / R) — the largest legal packet
//             at that R, hence highest OFF, hence lowest loss fraction.
//             Catches the "few big raids" front that phase 1's caps can't
//             reach when defBudget is high.
//
// Both phases write into the same dedup map; cheapest cost per bucket wins.
//
// inv        — { c, sp, a, pa, t } troop counts
// defZero    — highest DEF where comp must take zero casualties
// defBudget  — DEF at which losses are capped by `budget`
// budget     — max resource loss per raid at defBudget
// smithyLv   — { c, sp, a, pa, t } smithy levels 0–20 (optional)
export function findCompositions(inv, defZero, defBudget, budget, smithyLv) {
  const atks = buildAtks(smithyLv || {})
  if (totalUnits(inv) === 0) return EMPTY_RESULT

  const atkC  = atks[0], atkSp = atks[1], atkA = atks[2], atkPa = atks[3], atkT = atks[4]

  // Inline dedup: bucket by (raids, bottleneck, first-to-die, pattern).
  // Numeric key avoids string allocation in the hot path.
  const buckets = new Map()
  const DIES_NAMES = ['c', 'sp', 'a', 'pa', 't']

  // Closure that records a candidate into the bucket map. Both phases call
  // this with `n`, `raids`, `off`, `botMask`, `patternMask`, `diesIdx`,
  // `deployCostHere` already computed, then we run the expensive checks
  // (zero-cas, budget) gated by the bucket race.
  function tryRecord(c, sp, a, pa, t, n, raids, off, botMask, patternMask, diesIdx, deployCostHere) {
    const key = (raids & 0xFFFFFF) * 4096 + botMask * 128 + diesIdx * 16 + patternMask
    const existing = buckets.get(key)
    if (existing && existing.cost <= deployCostHere) return

    if (defZero > 0) {
      const rZ = defZero / off
      const lossZ = (rZ * Math.sqrt(rZ)) / (1 + rZ * Math.sqrt(rZ))
      const dZ =
        Math.round(c  * lossZ) +
        Math.round(sp * lossZ) +
        Math.round(a  * lossZ) +
        Math.round(pa * lossZ) +
        Math.round(t  * lossZ)
      if (dZ !== 0) return
    }

    const rO = defBudget / off
    const lossO = (rO * Math.sqrt(rO)) / (1 + rO * Math.sqrt(rO))
    const ddC  = Math.round(c  * lossO)
    const ddSp = Math.round(sp * lossO)
    const ddA  = Math.round(a  * lossO)
    const ddPa = Math.round(pa * lossO)
    const ddT  = Math.round(t  * lossO)
    const costAtBudget = ddC * 250 + ddSp * 340 + ddA * 490 + ddPa * 1005 + ddT * 1525
    if (costAtBudget > budget) return

    buckets.set(key, {
      p: { c, sp, a, pa, t },
      n, raids, off,
      cost: deployCostHere,
      dies: DIES_NAMES[diesIdx],
      costAtBudget,
      casCountAtBudget: ddC + ddSp + ddA + ddPa + ddT,
      maxZero: null,
      maxBudget: null,
      left: {
        c:  inv.c  - c  * raids,
        sp: inv.sp - sp * raids,
        a:  inv.a  - a  * raids,
        pa: inv.pa - pa * raids,
        t:  inv.t  - t  * raids,
      },
    })
  }

  // ── PHASE 1 ─ exhaustive small-packet sweep (gated by defZero > 0) ──
  if (defZero > 0) {
    const [maxC, maxSp, maxA, maxPa, maxT] = fitCapsToBudget([
      capRange(UNIT_BY_KEY.c,  inv.c),
      capRange(UNIT_BY_KEY.sp, inv.sp),
      capRange(UNIT_BY_KEY.a,  inv.a),
      capRange(UNIT_BY_KEY.pa, inv.pa),
      capRange(UNIT_BY_KEY.t,  inv.t),
    ], PHASE1_BUDGET)

    // Precomputed maxOFF from deeper loop levels, for subtree pruning.
    const maxOffAfterC  = maxSp * atkSp + maxA * atkA + maxPa * atkPa + maxT * atkT
    const maxOffAfterSp = maxA * atkA + maxPa * atkPa + maxT * atkT
    const maxOffAfterA  = maxPa * atkPa + maxT * atkT
    const maxOffAfterPa = maxT * atkT

    for (let c = 0; c <= maxC; c++) {
      const offC = c * atkC
      if (offC + maxOffAfterC <= defBudget) continue
      const rcMax = c === 0 ? Infinity : Math.floor(inv.c / c)

      for (let sp = 0; sp <= maxSp; sp++) {
        const offCSp = offC + sp * atkSp
        if (offCSp + maxOffAfterSp <= defBudget) continue
        const rspMax = sp === 0 ? Infinity : Math.floor(inv.sp / sp)
        const minRC_SP = Math.min(rcMax, rspMax)

        for (let a = 0; a <= maxA; a++) {
          const offCSpA = offCSp + a * atkA
          if (offCSpA + maxOffAfterA <= defBudget) continue
          const raMax = a === 0 ? Infinity : Math.floor(inv.a / a)
          const minRC_SPA = Math.min(minRC_SP, raMax)

          for (let pa = 0; pa <= maxPa; pa++) {
            const offBase = offCSpA + pa * atkPa
            if (offBase + maxOffAfterPa <= defBudget) continue
            const rpaMax = pa === 0 ? Infinity : Math.floor(inv.pa / pa)
            const minRC_SPAPa = Math.min(minRC_SPA, rpaMax)

            for (let t = 0; t <= maxT; t++) {
              const off = offBase + t * atkT
              const n = c + sp + a + pa + t
              if (n === 0) continue
              if (off <= defBudget) continue

              if (n === 1 && (
                (c  === 1 && SOLO_DOOMED.has('c'))  ||
                (sp === 1 && SOLO_DOOMED.has('sp')) ||
                (a  === 1 && SOLO_DOOMED.has('a'))  ||
                (pa === 1 && SOLO_DOOMED.has('pa'))
              )) continue

              const rtMax = t === 0 ? Infinity : Math.floor(inv.t / t)
              const raids = Math.min(minRC_SPAPa, rtMax)
              if (!isFinite(raids) || raids === 0) continue

              let botMask = 0
              if (c  > 0 && rcMax  === raids) botMask |= 1
              if (sp > 0 && rspMax === raids) botMask |= 2
              if (a  > 0 && raMax  === raids) botMask |= 4
              if (pa > 0 && rpaMax === raids) botMask |= 8
              if (t  > 0 && rtMax  === raids) botMask |= 16

              let diesIdx = 0, maxCnt = c
              if (sp > maxCnt) { maxCnt = sp; diesIdx = 1 }
              if (a  > maxCnt) { maxCnt = a;  diesIdx = 2 }
              if (pa > maxCnt) { maxCnt = pa; diesIdx = 3 }
              if (t  > maxCnt) { maxCnt = t;  diesIdx = 4 }

              const patternMask =
                (c  > 0 ? 1  : 0) |
                (sp > 0 ? 2  : 0) |
                (a  > 0 ? 4  : 0) |
                (pa > 0 ? 8  : 0) |
                (t  > 0 ? 16 : 0)

              const deployCostHere = c * 250 + sp * 340 + a * 490 + pa * 1005 + t * 1525
              tryRecord(c, sp, a, pa, t, n, raids, off, botMask, patternMask, diesIdx, deployCostHere)
            }
          }
        }
      }
    }
  }

  // ── PHASE 2 ─ R-based bottleneck-tight enumeration ──
  //
  // For each non-empty subset of unit types (mask) and each raid count R,
  // generate the packet pack_k = floor(inv_k / R) for chosen types. At a
  // fixed R any sub-bottleneck variant has strictly lower OFF with the
  // same raid count, so is Pareto-dominated on every strategy axis.
  // Iterations: 31 × min(inv) — typically a few thousand, each O(1).
  for (let mask = 1; mask < 32; mask++) {
    if ((mask & 1)  && inv.c  === 0) continue
    if ((mask & 2)  && inv.sp === 0) continue
    if ((mask & 4)  && inv.a  === 0) continue
    if ((mask & 8)  && inv.pa === 0) continue
    if ((mask & 16) && inv.t  === 0) continue

    let maxR = Infinity
    if (mask & 1)  maxR = Math.min(maxR, inv.c)
    if (mask & 2)  maxR = Math.min(maxR, inv.sp)
    if (mask & 4)  maxR = Math.min(maxR, inv.a)
    if (mask & 8)  maxR = Math.min(maxR, inv.pa)
    if (mask & 16) maxR = Math.min(maxR, inv.t)
    if (!isFinite(maxR) || maxR < 1) continue

    for (let R = 1; R <= maxR; R++) {
      const c  = (mask & 1)  ? (inv.c  / R | 0) : 0
      const sp = (mask & 2)  ? (inv.sp / R | 0) : 0
      const a  = (mask & 4)  ? (inv.a  / R | 0) : 0
      const pa = (mask & 8)  ? (inv.pa / R | 0) : 0
      const t  = (mask & 16) ? (inv.t  / R | 0) : 0

      // If floor() reduced a required slot to zero, the realised subset
      // shrinks — skip so it's handled by the smaller-mask iteration.
      if ((mask & 1)  && c  === 0) continue
      if ((mask & 2)  && sp === 0) continue
      if ((mask & 4)  && a  === 0) continue
      if ((mask & 8)  && pa === 0) continue
      if ((mask & 16) && t  === 0) continue

      const n = c + sp + a + pa + t
      if (n === 0) continue

      if (n === 1 && (
        (c  === 1 && SOLO_DOOMED.has('c'))  ||
        (sp === 1 && SOLO_DOOMED.has('sp')) ||
        (a  === 1 && SOLO_DOOMED.has('a'))  ||
        (pa === 1 && SOLO_DOOMED.has('pa'))
      )) continue

      const off = c * atkC + sp * atkSp + a * atkA + pa * atkPa + t * atkT
      if (off <= defBudget) continue

      // Realised raid count: floor() may have rounded so sharply that the
      // actual bottleneck is higher than R. Recompute from the packet.
      let raids = Infinity
      if (c  > 0) { const r = (inv.c  / c)  | 0; if (r < raids) raids = r }
      if (sp > 0) { const r = (inv.sp / sp) | 0; if (r < raids) raids = r }
      if (a  > 0) { const r = (inv.a  / a)  | 0; if (r < raids) raids = r }
      if (pa > 0) { const r = (inv.pa / pa) | 0; if (r < raids) raids = r }
      if (t  > 0) { const r = (inv.t  / t)  | 0; if (r < raids) raids = r }

      let botMask = 0
      if (c  > 0 && ((inv.c  / c)  | 0) === raids) botMask |= 1
      if (sp > 0 && ((inv.sp / sp) | 0) === raids) botMask |= 2
      if (a  > 0 && ((inv.a  / a)  | 0) === raids) botMask |= 4
      if (pa > 0 && ((inv.pa / pa) | 0) === raids) botMask |= 8
      if (t  > 0 && ((inv.t  / t)  | 0) === raids) botMask |= 16

      let diesIdx = 0, maxCnt = c
      if (sp > maxCnt) { maxCnt = sp; diesIdx = 1 }
      if (a  > maxCnt) { maxCnt = a;  diesIdx = 2 }
      if (pa > maxCnt) { maxCnt = pa; diesIdx = 3 }
      if (t  > maxCnt) { maxCnt = t;  diesIdx = 4 }

      const patternMask =
        (c  > 0 ? 1  : 0) |
        (sp > 0 ? 2  : 0) |
        (a  > 0 ? 4  : 0) |
        (pa > 0 ? 8  : 0) |
        (t  > 0 ? 16 : 0)

      const deployCostHere = c * 250 + sp * 340 + a * 490 + pa * 1005 + t * 1525
      tryRecord(c, sp, a, pa, t, n, raids, off, botMask, patternMask, diesIdx, deployCostHere)
    }
  }

  const unique = Array.from(buckets.values())
  if (!unique.length) return EMPTY_RESULT

  // Deferred max-DEF binary searches — only for unique survivors.
  for (const v of unique) {
    v.maxZero   = maxDefForCasualties(v.p, 0, atks)
    v.maxBudget = maxDefForBudget(v.p, budget, atks)
  }

  const result = scoreStrategies(unique)
  // Attach the smithy-adjusted atks so callers don't recompute.
  result.atks = atks
  return result
}

// Pull a Teuton inventory dict out of the /military/troops API response
// (which uses Travian unit ids t1..t10).
export function inventoryFromTroopsAPI(troops) {
  return {
    c:  Number(troops?.t1 || 0),
    sp: Number(troops?.t2 || 0),
    a:  Number(troops?.t3 || 0),
    pa: Number(troops?.t5 || 0),
    t:  Number(troops?.t6 || 0),
  }
}

// Pull a Teuton smithy-level dict out of the /military/smithy API response
// (which uses Travian unit ids t1..t10 under a `research` key).
export function smithyFromAPI(payload) {
  const r = payload?.research || {}
  const clamp = (n) => Math.min(20, Math.max(0, Number(n) || 0))
  return {
    c:  clamp(r.t1),
    sp: clamp(r.t2),
    a:  clamp(r.t3),
    pa: clamp(r.t5),
    t:  clamp(r.t6),
  }
}
