import { Scale, Shape } from "@visx/visx";
import { type ReactNode, useMemo, useState } from "react";

const MARGIN = { top: 12, right: 16, bottom: 28, left: 48 };

function ChartShell({
  title,
  chart,
  table,
}: {
  title?: string;
  chart: ReactNode;
  table: ReactNode;
}) {
  const [asTable, setAsTable] = useState(false);
  return (
    <figure className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        {title ? (
          <figcaption className="text-xs text-(--color-neutral-600)">{title}</figcaption>
        ) : (
          <span />
        )}
        <button
          type="button"
          className="btn btn-ghost text-xs"
          aria-pressed={asTable}
          onClick={() => setAsTable((v) => !v)}
        >
          {asTable ? "View as chart" : "View as table"}
        </button>
      </div>
      {asTable ? table : chart}
    </figure>
  );
}

function Tooltip({ x, y, children }: { x: number; y: number; children: ReactNode }) {
  return (
    <div
      role="tooltip"
      className="pointer-events-none absolute z-10 border border-(--color-divider) bg-(--color-surface) px-2 py-1 text-xs shadow-(--shadow-md)"
      style={{ left: x + 8, top: y - 8 }}
    >
      {children}
    </div>
  );
}

export interface LinePoint {
  /** Category label (e.g. ISO date). */
  x: string;
  /** Value; `null` breaks the line. */
  y: number | null;
}

export function LineChart({
  data,
  title,
  height = 220,
  width = 560,
  formatY = (v: number) => v.toFixed(2),
}: {
  data: LinePoint[];
  title?: string;
  height?: number;
  width?: number;
  formatY?: (v: number) => string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const innerW = width - MARGIN.left - MARGIN.right;
  const innerH = height - MARGIN.top - MARGIN.bottom;

  const { xScale, yScale, defined } = useMemo(() => {
    const xs = data.map((d) => d.x);
    const ys = data.map((d) => d.y).filter((y): y is number => y !== null);
    const yMin = ys.length ? Math.min(...ys) : 0;
    const yMax = ys.length ? Math.max(...ys) : 1;
    const pad = (yMax - yMin) * 0.05 || 1;
    return {
      xScale: Scale.scalePoint<string>({
        domain: xs,
        range: [MARGIN.left, MARGIN.left + innerW],
        padding: 0.5,
      }),
      yScale: Scale.scaleLinear<number>({
        domain: [yMin - pad, yMax + pad],
        range: [MARGIN.top + innerH, MARGIN.top],
        nice: true,
      }),
      defined: data.filter((d): d is LinePoint & { y: number } => d.y !== null),
    };
  }, [data, innerW, innerH]);

  const onMove = (event: React.MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const mx = event.clientX - rect.left;
    let best = -1;
    let bestDist = Infinity;
    data.forEach((d, i) => {
      const px = xScale(d.x);
      const dist = px === undefined ? Infinity : Math.abs(px - mx);
      if (dist < bestDist) {
        bestDist = dist;
        best = i;
      }
    });
    setHover(bestDist < innerW / data.length ? best : null);
  };

  const chart = (
    <div className="relative">
      <svg
        width={width}
        height={height}
        role="img"
        aria-label={title ?? "Line chart"}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        {/* y gridlines + labels */}
        {yScale.ticks(4).map((t) => (
          <g key={t}>
            <line
              x1={MARGIN.left}
              x2={MARGIN.left + innerW}
              y1={yScale(t)}
              y2={yScale(t)}
              stroke="var(--color-divider)"
              strokeWidth={1}
            />
            <text
              x={MARGIN.left - 6}
              y={yScale(t) + 3}
              textAnchor="end"
              fontSize={10}
              fill="var(--color-neutral-600)"
            >
              {formatY(t)}
            </text>
          </g>
        ))}
        {xScale.domain().length > 0 &&
          [xScale.domain()[0], xScale.domain()[xScale.domain().length - 1]].map((x) => {
            const px = xScale(x);
            return px === undefined ? null : (
              <text
                key={x}
                x={px}
                y={height - 8}
                textAnchor="middle"
                fontSize={10}
                fill="var(--color-neutral-600)"
              >
                {x}
              </text>
            );
          })}
        <Shape.LinePath
          data={defined}
          x={(d) => xScale(d.x) ?? 0}
          y={(d) => yScale(d.y)}
          stroke="var(--color-accent)"
          strokeWidth={2}
        />
        {defined.map((d) => {
          const px = xScale(d.x);
          const py = yScale(d.y);
          return px === undefined ? null : (
            <circle key={d.x} cx={px} cy={py} r={3} fill="var(--color-accent)" />
          );
        })}
        {data.length === 0 && (
          <text
            x={width / 2}
            y={height / 2}
            textAnchor="middle"
            fontSize={12}
            fill="var(--color-neutral-500)"
          >
            No data
          </text>
        )}
      </svg>
      {hover !== null && data[hover] && data[hover].y !== null && (
        <Tooltip x={xScale(data[hover].x) ?? 0} y={yScale(data[hover].y as number)}>
          {data[hover].x}: {formatY(data[hover].y as number)}
        </Tooltip>
      )}
    </div>
  );

  const table = (
    <table className="table">
      <thead>
        <tr>
          <th scope="col">x</th>
          <th scope="col">y</th>
        </tr>
      </thead>
      <tbody>
        {data.map((d) => (
          <tr key={d.x}>
            <td>{d.x}</td>
            <td>{d.y === null ? "—" : formatY(d.y)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );

  return <ChartShell title={title} chart={chart} table={table} />;
}

export interface BarDatum {
  /** Category label. */
  x: string;
  y: number;
}

export function BarChart({
  data,
  title,
  height = 220,
  width = 560,
  formatY = (v: number) => v.toFixed(2),
}: {
  data: BarDatum[];
  title?: string;
  height?: number;
  width?: number;
  formatY?: (v: number) => string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const innerW = width - MARGIN.left - MARGIN.right;
  const innerH = height - MARGIN.top - MARGIN.bottom;

  const { xScale, yScale } = useMemo(() => {
    const ys = data.map((d) => d.y);
    const yMin = ys.length ? Math.min(0, ...ys) : 0;
    const yMax = ys.length ? Math.max(0, ...ys) : 1;
    const pad = (yMax - yMin) * 0.05 || 1;
    return {
      xScale: Scale.scaleBand<string>({
        domain: data.map((d) => d.x),
        range: [MARGIN.left, MARGIN.left + innerW],
        padding: 0.2,
      }),
      yScale: Scale.scaleLinear<number>({
        domain: [yMin - pad, yMax + pad],
        range: [MARGIN.top + innerH, MARGIN.top],
        nice: true,
      }),
    };
  }, [data, innerW, innerH]);

  const zeroY = yScale(0);

  const chart = (
    <div className="relative">
      <svg width={width} height={height} role="img" aria-label={title ?? "Bar chart"}>
        <line
          x1={MARGIN.left}
          x2={MARGIN.left + innerW}
          y1={zeroY}
          y2={zeroY}
          stroke="var(--color-divider)"
        />
        {data.map((d, i) => {
          const bx = xScale(d.x);
          const bw = xScale.bandwidth();
          const by = Math.min(yScale(d.y), zeroY);
          const bh = Math.abs(yScale(d.y) - zeroY);
          if (bx === undefined) return null;
          return (
            // biome-ignore lint/a11y/noStaticElementInteractions: svg bar hover target, tooltip is supplementary (table fallback exists)
            <g key={d.x} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
              <rect
                x={bx}
                y={by}
                width={bw}
                height={bh}
                fill={hover === i ? "var(--color-accent-600)" : "var(--color-accent)"}
              />
              <text
                x={bx + bw / 2}
                y={height - 8}
                textAnchor="middle"
                fontSize={10}
                fill="var(--color-neutral-600)"
              >
                {d.x}
              </text>
            </g>
          );
        })}
        {data.length === 0 && (
          <text
            x={width / 2}
            y={height / 2}
            textAnchor="middle"
            fontSize={12}
            fill="var(--color-neutral-500)"
          >
            No data
          </text>
        )}
      </svg>
      {hover !== null && data[hover] && (
        <Tooltip
          x={(xScale(data[hover].x) ?? 0) + xScale.bandwidth() / 2}
          y={yScale(data[hover].y)}
        >
          {data[hover].x}: {formatY(data[hover].y)}
        </Tooltip>
      )}
    </div>
  );

  const table = (
    <table className="table">
      <thead>
        <tr>
          <th scope="col">x</th>
          <th scope="col">y</th>
        </tr>
      </thead>
      <tbody>
        {data.map((d) => (
          <tr key={d.x}>
            <td>{d.x}</td>
            <td>{formatY(d.y)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );

  return <ChartShell title={title} chart={chart} table={table} />;
}
