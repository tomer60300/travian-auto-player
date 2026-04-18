import { useMemo, useState } from 'react'

export default function ScanPreviewTable({ data }) {
  const [sort, setSort] = useState({ field: 'assigned_bucket', dir: 'asc' })
  const [search, setSearch] = useState('')

  const rows = data?.records || []
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    const list = !q ? rows : rows.filter((r) =>
      (r.player_name || '').toLowerCase().includes(q) ||
      (r.alliance_tag || '').toLowerCase().includes(q) ||
      `${r.x},${r.y}`.includes(q) ||
      (r.assigned_bucket || '').toLowerCase().includes(q)
    )
    const { field, dir } = sort
    const sign = dir === 'asc' ? 1 : -1
    return [...list].sort((a, b) => {
      const av = a[field] ?? ''
      const bv = b[field] ?? ''
      if (typeof av === 'number' && typeof bv === 'number') return sign * (av - bv)
      return sign * String(av).localeCompare(String(bv))
    })
  }, [rows, search, sort])

  const toggleSort = (field) => {
    setSort((s) => s.field === field ? { field, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { field, dir: 'asc' })
  }

  const sortArrow = (field) => sort.field === field ? (sort.dir === 'asc' ? '↑' : '↓') : ''

  return (
    <div className="card mb-4">
      <h3 className="heading-gold text-lg mb-3">Scan preview</h3>

      <div className="mb-4 grid grid-cols-3 gap-4 text-sm">
        <div>
          <div className="text-secondary">Total scanned</div>
          <div className="text-lg font-semibold">{data?.total_scanned ?? 0}</div>
        </div>
        <div>
          <div className="text-secondary">Enriched</div>
          <div className="text-lg font-semibold">{data?.enriched ?? 0}</div>
        </div>
        <div>
          <div className="text-secondary">Survivors</div>
          <div className="text-lg font-semibold text-success">{data?.survivors ?? 0}</div>
        </div>
      </div>

      {data?.drop_counts && Object.keys(data.drop_counts).length > 0 && (
        <div className="mb-4">
          <div className="font-semibold mb-1 text-sm">Filter drops:</div>
          <div className="flex gap-2 flex-wrap">
            {Object.entries(data.drop_counts).map(([reason, n]) => (
              <span key={reason} className="loop-chip">{reason}: {n}</span>
            ))}
          </div>
        </div>
      )}

      {data?.bucket_counts && Object.keys(data.bucket_counts).length > 0 && (
        <div className="mb-4">
          <div className="font-semibold mb-1 text-sm">Per-bucket counts:</div>
          <div className="flex gap-2 flex-wrap">
            {Object.entries(data.bucket_counts).map(([name, n]) => (
              <span key={name} className="loop-chip">{name}: {n}</span>
            ))}
          </div>
        </div>
      )}

      <div className="mb-2">
        <input
          type="text"
          className="input-field w-full"
          placeholder="Search players, alliance, coords, bucket..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      <div className="overflow-auto" style={{ maxHeight: 400 }}>
        <table className="data-table w-full text-sm">
          <thead>
            <tr>
              <th className="cursor-pointer" onClick={() => toggleSort('x')}>coord {sortArrow('x')}</th>
              <th className="cursor-pointer" onClick={() => toggleSort('player_name')}>player {sortArrow('player_name')}</th>
              <th className="cursor-pointer" onClick={() => toggleSort('alliance_tag')}>alliance {sortArrow('alliance_tag')}</th>
              <th className="cursor-pointer" onClick={() => toggleSort('target_village_pop')}>v.pop {sortArrow('target_village_pop')}</th>
              <th className="cursor-pointer" onClick={() => toggleSort('player_total_pop')}>p.total {sortArrow('player_total_pop')}</th>
              <th className="cursor-pointer" onClick={() => toggleSort('assigned_bucket')}>bucket {sortArrow('assigned_bucket')}</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r, i) => (
              <tr key={i}>
                <td>({r.x}|{r.y})</td>
                <td>{r.player_name || '-'}</td>
                <td>{r.alliance_tag || '-'}</td>
                <td>{r.target_village_pop}</td>
                <td>{r.player_total_pop}</td>
                <td>{r.assigned_bucket}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="text-xs text-secondary mt-1">Showing {filtered.length} of {rows.length}</div>
    </div>
  )
}
