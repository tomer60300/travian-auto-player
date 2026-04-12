/**
 * SkeletonRow — shimmer placeholder for loading table/list rows.
 *
 * Props:
 * @param {number} [columns=4] - number of shimmer blocks per row
 * @param {number} [rows=5] - number of skeleton rows to render
 */
export default function SkeletonRow({ columns = 4, rows = 5 }) {
  return (
    <div className="flex flex-col gap-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex gap-3 animate-slide-in" style={{ animationDelay: `${i * 50}ms` }}>
          {Array.from({ length: columns }).map((_, j) => (
            <div
              key={j}
              className="skeleton"
              style={{
                height: 16,
                flex: j === 0 ? '0 0 60px' : '1',
                borderRadius: 4,
              }}
            />
          ))}
        </div>
      ))}
    </div>
  )
}
