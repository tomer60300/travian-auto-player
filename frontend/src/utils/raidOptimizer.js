// Raid Composition Optimizer — pure math, no DOM/React.
//
// Designs the cheapest force per raid that can hit a given DEF threshold
// without losses (or with at most 1 loss), and ranks compositions by
// parallel raid count → casualty cost → deploy cost.
//
// Currently Teuton-only (5 unit types). Server speed is irrelevant for
// the combat formula — speed only changes troop production rate, not
// the OFF/DEF balance.

export const K = 1.5

// Order matters for the deterministic loops below; sorted by ascending
// resource cost so cheapest deaths appear first when the algorithm is
// otherwise indifferent.
export const UNITS = [
  { key: 'c',  name: 'Clubswinger',     atk:  40, cost:  250, carry: 60,  troopId: 't1' },
  { key: 'sp', name: 'Spearman',        atk:  10, cost:  340, carry: 40,  troopId: 't2' },
  { key: 'a',  name: 'Axeman',          atk:  60, cost:  490, carry: 50,  troopId: 't3' },
  { key: 'pa', name: 'Paladin',         atk:  55, cost: 1005, carry: 110, troopId: 't5' },
  { key: 't',  name: 'Teutonic Knight', atk: 150, cost: 1525, carry: 80,  troopId: 't6' },
]

export const UNIT_BY_KEY = Object.fromEntries(UNITS.map((u) => [u.key, u]))

// Lone-attacker rule: any single unit with ATK < 83 dies even on an
// empty target. Only solo TK survives.
const SOLO_DOOMED = new Set(UNITS.filter((u) => u.atk < 83).map((u) => u.key))

// ── Combat math ────────────────────────────────────────────────────

export function offOf(p) {
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
  const x = Math.pow(def / off, K)
  return x / (1 + x)
}

export function isWipe(p, def) {
  const off = offOf(p)
  const n = totalUnits(p)
  if (n === 1) {
    for (const u of UNITS) {
      if (p[u.key] === 1 && SOLO_DOOMED.has(u.key)) return true
    }
  }
  return off <= def
}

export function casualties(p, def) {
  const off = offOf(p)
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

export function totalDead(dead) {
  return dead.c + dead.sp + dead.a + dead.pa + dead.t
}

// Binary search for the highest DEF where total casualties ≤ maxCas
// AND the raid isn't a wipe. Used for the "Max DEF · 0/1 cas" displays.
export function maxDefForCasualties(p, maxCas) {
  let lo = 1, hi = 2000, best = 0
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (isWipe(p, mid)) { hi = mid - 1; continue }
    const cas = totalDead(casualties(p, mid))
    if (cas <= maxCas) { best = mid; lo = mid + 1 }
    else hi = mid - 1
  }
  return best
}

// First unit to die is the unit with the highest count (all share the
// same loss fraction; round(N×loss) crosses 0.5 first for the largest N).
function firstToDie(p) {
  let max = 0, key = null
  for (const u of UNITS) {
    if (p[u.key] > max) { max = p[u.key]; key = u.key }
  }
  return key
}

function casualtyCost(diesKey) {
  if (diesKey == null) return 0
  return UNIT_BY_KEY[diesKey].cost
}

// ── Search ────────────────────────────────────────────────────────

export function findCompositions(inv, defZero, defOne) {
  const valid = []
  const total = inv.c + inv.sp + inv.a + inv.pa + inv.t

  // Adaptive cap by ATK class + inventory share. Without this, the loop
  // is O(maxC × maxSp × maxA × maxPa × maxT) — exponential blowup. With
  // it, hot path stays under ~100k iterations even with 500+ of each type.
  function cap(unit, invK) {
    if (total === 0 || invK === 0) return 0
    const ratio = invK / total
    let base
    if (unit.atk >= 100)      base = 35
    else if (unit.atk >= 50)  base = 25
    else                      base = 12
    if (ratio > 0.7)      base = Math.max(base, 250)
    else if (ratio > 0.4) base = Math.max(base, 80)
    return Math.min(invK, base)
  }
  const maxC  = cap(UNIT_BY_KEY.c,  inv.c)
  const maxSp = cap(UNIT_BY_KEY.sp, inv.sp)
  const maxA  = cap(UNIT_BY_KEY.a,  inv.a)
  const maxPa = cap(UNIT_BY_KEY.pa, inv.pa)
  const maxT  = cap(UNIT_BY_KEY.t,  inv.t)

  for (let c = 0; c <= maxC; c++) {
    const offC = c * 40
    for (let sp = 0; sp <= maxSp; sp++) {
      const offCSp = offC + sp * 10
      for (let a = 0; a <= maxA; a++) {
        const offCSpA = offCSp + a * 60
        for (let pa = 0; pa <= maxPa; pa++) {
          const offBase = offCSpA + pa * 55
          for (let t = 0; t <= maxT; t++) {
            const off = offBase + t * 150
            const n = c + sp + a + pa + t
            if (n === 0) continue
            if (off <= defOne) continue

            if (n === 1) {
              if ((c === 1 && SOLO_DOOMED.has('c')) ||
                  (sp === 1 && SOLO_DOOMED.has('sp')) ||
                  (a === 1 && SOLO_DOOMED.has('a')) ||
                  (pa === 1 && SOLO_DOOMED.has('pa'))) continue
            }

            // K=1.5 ⇒ x = r·√r ; ~3× faster than Math.pow.
            let dZ = 0
            if (defZero > 0) {
              const rZ = defZero / off
              const xZ = rZ * Math.sqrt(rZ)
              const lossZ = xZ / (1 + xZ)
              dZ = Math.round(c * lossZ) + Math.round(sp * lossZ) + Math.round(a * lossZ) +
                   Math.round(pa * lossZ) + Math.round(t * lossZ)
            }
            if (dZ !== 0) continue

            const rO = defOne / off
            const xO = rO * Math.sqrt(rO)
            const lossO = xO / (1 + xO)
            const dO = Math.round(c * lossO) + Math.round(sp * lossO) + Math.round(a * lossO) +
                       Math.round(pa * lossO) + Math.round(t * lossO)
            if (dO > 1) continue

            const rc  = c  === 0 ? Infinity : Math.floor(inv.c  / c)
            const rsp = sp === 0 ? Infinity : Math.floor(inv.sp / sp)
            const ra  = a  === 0 ? Infinity : Math.floor(inv.a  / a)
            const rpa = pa === 0 ? Infinity : Math.floor(inv.pa / pa)
            const rt  = t  === 0 ? Infinity : Math.floor(inv.t  / t)
            const raids = Math.min(rc, rsp, ra, rpa, rt)
            if (!isFinite(raids) || raids === 0) continue

            const p = { c, sp, a, pa, t }
            valid.push({
              p, n, raids, off,
              cost: deployCost(p),
              dies: firstToDie(p),
              maxZero: null, maxOne: null,
              left: {
                c: inv.c - c * raids, sp: inv.sp - sp * raids,
                a: inv.a - a * raids, pa: inv.pa - pa * raids, t: inv.t - t * raids,
              },
            })
          }
        }
      }
    }
  }

  const sortFn = (x, y) => {
    if (y.raids !== x.raids) return y.raids - x.raids
    const xd = casualtyCost(x.dies), yd = casualtyCost(y.dies)
    if (xd !== yd) return xd - yd
    return x.cost - y.cost
  }
  valid.sort(sortFn)

  // Diversify by structural signature so alternatives aren't variations
  // of the same shape.
  const buckets = new Map()
  for (const v of valid) {
    const rPer = (cnt, invK) => (cnt === 0 ? Infinity : Math.floor(invK / cnt))
    const rs = UNITS.map((u) => rPer(v.p[u.key], inv[u.key]))
    let bot = ''
    UNITS.forEach((u, i) => { if (rs[i] === v.raids) bot += u.key })
    const pattern = UNITS.map((u) => (v.p[u.key] > 0 ? u.key : '_')).join('')
    const key = `${v.raids}-${bot}-${v.dies || 'none'}-${pattern}`
    const existing = buckets.get(key)
    if (!existing || v.cost < existing.cost) buckets.set(key, v)
  }
  const unique = Array.from(buckets.values())
  unique.sort(sortFn)
  const top = unique.slice(0, 7)

  // Expensive max-DEF binary searches deferred to the displayed candidates only.
  for (const v of top) {
    v.maxZero = maxDefForCasualties(v.p, 0)
    v.maxOne  = maxDefForCasualties(v.p, 1)
  }
  return top
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
