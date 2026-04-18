const RULE_FIELDS = [
  { value: 'target_village_pop', label: 'Target village pop', type: 'number', defaultOp: '<=', defaultVal: 120 },
  { value: 'player_total_pop', label: 'Player total pop', type: 'number', defaultOp: '<=', defaultVal: 220 },
  { value: 'alliance_tag', label: 'Alliance tag', type: 'tag', defaultOp: 'not in', defaultVal: [] },
  { value: 'player_name', label: 'Player name', type: 'tag', defaultOp: 'not in', defaultVal: [] },
]

const NUM_OPS = ['<=', '<', '=', '!=', '>=', '>']
const TAG_OPS = ['in', 'not in', '=', '!=']

export default function AdvancedSpec({ villages, value, onChange, disabled, radius }) {
  const homes = villages.filter((v) => value.home_village_ids.includes(v.id))
  // per_home_lists: { [village_id]: [ { name, rules: [{field,op,value}] } ] }
  const perHome = value.per_home_lists || {}

  const setPerHome = (newPerHome) => {
    onChange({ ...value, per_home_lists: newPerHome })
  }

  // Auto-init per-home lists when a home village is selected but has no lists
  const ensureHome = (vid) => {
    if (perHome[vid] && perHome[vid].length > 0) return perHome
    const home = villages.find((v) => v.id === vid)
    const short = home?.name || `V${vid}`
    const r = radius || 30
    return {
      ...perHome,
      [vid]: [
        { name: `${short}-S-${r}`, rules: [{ field: 'target_village_pop', op: '<=', value: 120 }] },
        { name: `${short}-M-${r}`, rules: [] },
      ],
    }
  }

  // Make sure all selected homes have entries
  const effectivePerHome = { ...perHome }
  for (const h of homes) {
    if (!effectivePerHome[h.id] || effectivePerHome[h.id].length === 0) {
      const inited = ensureHome(h.id)
      effectivePerHome[h.id] = inited[h.id]
    }
  }
  // Sync to state if we auto-initialized
  if (JSON.stringify(effectivePerHome) !== JSON.stringify(perHome)) {
    // defer to avoid render-during-render
    setTimeout(() => setPerHome(effectivePerHome), 0)
  }

  const addList = (vid) => {
    const lists = [...(effectivePerHome[vid] || [])]
    const home = villages.find((v) => v.id === vid)
    const short = home?.name || `V${vid}`
    lists.push({ name: `${short}-${lists.length + 1}-${radius || 30}`, rules: [] })
    setPerHome({ ...effectivePerHome, [vid]: lists })
  }

  const removeList = (vid, idx) => {
    const lists = [...(effectivePerHome[vid] || [])]
    if (lists.length <= 1) return
    lists.splice(idx, 1)
    setPerHome({ ...effectivePerHome, [vid]: lists })
  }

  const updateList = (vid, idx, patch) => {
    const lists = [...(effectivePerHome[vid] || [])]
    lists[idx] = { ...lists[idx], ...patch }
    setPerHome({ ...effectivePerHome, [vid]: lists })
  }

  const addRule = (vid, listIdx) => {
    const lists = [...(effectivePerHome[vid] || [])]
    const rules = [...(lists[listIdx].rules || [])]
    rules.push({ field: 'target_village_pop', op: '<=', value: 120 })
    lists[listIdx] = { ...lists[listIdx], rules }
    setPerHome({ ...effectivePerHome, [vid]: lists })
  }

  const removeRule = (vid, listIdx, ruleIdx) => {
    const lists = [...(effectivePerHome[vid] || [])]
    const rules = [...(lists[listIdx].rules || [])]
    rules.splice(ruleIdx, 1)
    lists[listIdx] = { ...lists[listIdx], rules }
    setPerHome({ ...effectivePerHome, [vid]: lists })
  }

  const updateRule = (vid, listIdx, ruleIdx, patch) => {
    const lists = [...(effectivePerHome[vid] || [])]
    const rules = [...(lists[listIdx].rules || [])]
    rules[ruleIdx] = { ...rules[ruleIdx], ...patch }
    lists[listIdx] = { ...lists[listIdx], rules }
    setPerHome({ ...effectivePerHome, [vid]: lists })
  }

  const getField = (f) => RULE_FIELDS.find((x) => x.value === f)
  const fieldType = (f) => getField(f)?.type || 'number'

  const onFieldChange = (vid, listIdx, ruleIdx, newField) => {
    const def = getField(newField)
    updateRule(vid, listIdx, ruleIdx, {
      field: newField,
      op: def?.defaultOp || '<=',
      value: def?.defaultVal ?? '',
    })
  }

  const renderValueInput = (rule, vid, listIdx, ruleIdx) => {
    const t = fieldType(rule.field)
    if (t === 'number') {
      return (
        <input
          type="number"
          className="input-field"
          style={{ width: 100 }}
          value={typeof rule.value === 'number' ? rule.value : ''}
          onChange={(e) => updateRule(vid, listIdx, ruleIdx, { value: Number(e.target.value) })}
          disabled={disabled}
        />
      )
    }
    // tag: in / not in → comma list
    if (rule.op === 'in' || rule.op === 'not in') {
      const arr = Array.isArray(rule.value) ? rule.value : []
      return (
        <input
          type="text"
          className="input-field"
          style={{ minWidth: 160 }}
          placeholder="comma-separated, e.g. LR,HM"
          value={arr.join(', ')}
          onChange={(e) => updateRule(vid, listIdx, ruleIdx, {
            value: e.target.value.split(',').map((s) => s.trim()).filter(Boolean),
          })}
          disabled={disabled}
        />
      )
    }
    return (
      <input
        type="text"
        className="input-field"
        style={{ minWidth: 120 }}
        placeholder="value"
        value={typeof rule.value === 'string' ? rule.value : String(rule.value ?? '')}
        onChange={(e) => updateRule(vid, listIdx, ruleIdx, { value: e.target.value })}
        disabled={disabled}
      />
    )
  }

  const allowedOps = (rule) => {
    const t = fieldType(rule.field)
    if (t === 'tag') return TAG_OPS
    return NUM_OPS
  }

  // Count total lists across all homes
  const totalLists = Object.values(effectivePerHome).reduce((s, arr) => s + (arr?.length || 0), 0)

  return (
    <div className="card mb-4">
      <h3 className="heading-gold text-lg mb-2">2. Farm List Spec</h3>
      <p className="text-sm text-secondary mb-4">
        Each target is assigned to its <strong>closest selected home village</strong> (Chebyshev distance).
        Then it matches against that village's farm lists in order (first match wins).
        Villages not selected above are excluded — targets near them go to the next closest.
      </p>

      {homes.length === 0 && (
        <div className="error-box mb-2">Select at least one home village above to configure lists.</div>
      )}

      {homes.map((home) => {
        const vid = home.id
        const lists = effectivePerHome[vid] || []

        return (
          <div key={vid} className="mb-5">
            <div className="flex items-center gap-2 mb-3 pb-2 border-b border-default">
              <span className="text-lg">🏠</span>
              <span className="font-semibold text-primary">{home.name}</span>
              <span className="text-secondary text-sm">({home.x}|{home.y})</span>
              <span className="text-xs text-secondary ml-auto">{lists.length} list(s)</span>
              <button
                className="btn-secondary btn-xs"
                onClick={() => addList(vid)}
                disabled={disabled}
              >+ Add list</button>
            </div>

            {lists.map((list, li) => (
              <div key={li} className="border border-default rounded p-3 mb-2 ml-4">
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-xs text-secondary font-semibold" style={{ minWidth: 24 }}>
                    #{li + 1}
                  </span>
                  <div className="flex-1">
                    <input
                      type="text"
                      className="input-field w-full"
                      placeholder="Farm list name"
                      value={list.name || ''}
                      onChange={(e) => updateList(vid, li, { name: e.target.value })}
                      disabled={disabled}
                    />
                  </div>
                  {lists.length > 1 && (
                    <button
                      className="btn-danger btn-xs"
                      onClick={() => removeList(vid, li)}
                      disabled={disabled}
                    >Remove</button>
                  )}
                </div>

                <div className="pl-8 mb-1">
                  {(list.rules || []).length === 0 && (
                    <p className="text-xs text-secondary italic mb-1">
                      No rules — catches all remaining targets for this village.
                    </p>
                  )}
                  {(list.rules || []).map((rule, qi) => (
                    <div key={qi} className="flex items-center gap-2 mb-2 flex-wrap">
                      <span className="text-xs text-secondary font-semibold" style={{ minWidth: 44 }}>
                        {qi === 0 ? 'WHERE' : 'AND'}
                      </span>
                      <select
                        className="input-field"
                        style={{ minWidth: 160 }}
                        value={rule.field}
                        onChange={(e) => onFieldChange(vid, li, qi, e.target.value)}
                        disabled={disabled}
                      >
                        {RULE_FIELDS.map((f) => (
                          <option key={f.value} value={f.value}>{f.label}</option>
                        ))}
                      </select>
                      <select
                        className="input-field"
                        style={{ minWidth: 60 }}
                        value={rule.op}
                        onChange={(e) => updateRule(vid, li, qi, { op: e.target.value })}
                        disabled={disabled}
                      >
                        {allowedOps(rule).map((o) => (
                          <option key={o} value={o}>{o}</option>
                        ))}
                      </select>
                      {renderValueInput(rule, vid, li, qi)}
                      <button
                        className="btn-danger btn-xs"
                        onClick={() => removeRule(vid, li, qi)}
                        disabled={disabled}
                      >×</button>
                    </div>
                  ))}
                  <button
                    className="btn-secondary btn-xs"
                    onClick={() => addRule(vid, li)}
                    disabled={disabled}
                  >+ Add rule</button>
                </div>
              </div>
            ))}
          </div>
        )
      })}

      {homes.length > 0 && (
        <div className="mt-3 p-3 border border-default rounded bg-primary/5 text-sm">
          <strong>Summary:</strong> {homes.length} home village(s), {totalLists} farm list(s) total.
          Targets assigned to closest home, then matched top-down within each village's lists.
        </div>
      )}
    </div>
  )
}
