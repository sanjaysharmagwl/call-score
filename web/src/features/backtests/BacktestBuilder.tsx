import { type FormEvent, useMemo, useState } from "react";
import { formatApiError } from "../methodologies/api";
import { useAnalysts, useSectors } from "../shared/queries";
import { useActiveRun } from "../shared/run-state";
import { type BacktestSubmitResponse, useBenchmarks, useSubmitBacktest } from "./api";
import {
  type BacktestFormState,
  buildBacktestRequest,
  defaultBacktestForm,
  HORIZON_MODES,
  PEER_GROUP_DIMS,
  PORTFOLIO_MODES,
  REBALANCE_FREQS,
  SCORE_FIELDS,
  SELECTION_MODES,
  SIZING_MODES,
  validateBacktestForm,
} from "./spec";

function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string;
}) {
  // Caption + control; every control inside carries its own aria-label, so a
  // plain div avoids invalid <label> nesting for composite children.
  return (
    <div className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
      <span>{label}</span>
      {children}
      {hint && <span className="text-[11px] text-(--color-neutral-500)">{hint}</span>}
    </div>
  );
}

function NumInput({
  value,
  onChange,
  placeholder,
  ariaLabel,
  step,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  ariaLabel: string;
  step?: string;
}) {
  return (
    <input
      className="input"
      type="number"
      step={step}
      value={value}
      placeholder={placeholder}
      aria-label={ariaLabel}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <fieldset className="flex flex-col gap-3 border border-(--color-divider) p-3">
      <legend className="px-1 text-xs font-semibold uppercase tracking-[0.08em] text-(--color-neutral-600)">
        {title}
      </legend>
      {children}
    </fieldset>
  );
}

/**
 * Backtest builder: grouped form mirroring `BacktestSpec`. Client validation
 * mirrors the engine's `coherent` validator; server 422 detail is shown
 * verbatim. On 202 the caller redirects to the detail page which polls.
 */
export function BacktestBuilder({
  onSubmitted,
  onCancel,
}: {
  onSubmitted?: (result: BacktestSubmitResponse) => void;
  onCancel?: () => void;
}) {
  const [state, setState] = useState<BacktestFormState>(defaultBacktestForm);
  const [clientErrors, setClientErrors] = useState<string[]>([]);
  const submit = useSubmitBacktest();

  // Analyst cohort options come from the active run (backtests are not
  // run-scoped, but the signal pool is the analysts knowable in that run's
  // rating events). `useActiveRun` resolves `run_id=latest succeeded`.
  const { runId, isDefaulting } = useActiveRun();
  const analysts = useAnalysts({ run_id: runId ?? "", limit: 500, order: "asc" }, Boolean(runId));
  const analystItems = analysts.data?.items ?? [];
  const peerGroupOptions = useMemo(
    () =>
      [
        ...new Set(analystItems.map((a) => a.peer_group).filter((g): g is string => Boolean(g))),
      ].sort(),
    [analystItems],
  );

  const sectors = useSectors({ limit: 500 });
  const sectorOptions = sectors.data?.data ?? [];

  const benchmarks = useBenchmarks({ limit: 200 });
  const readyBenchmarks = (benchmarks.data?.data ?? []).filter((b) => b.status === "ready");

  const patch = (partial: Partial<BacktestFormState>) =>
    setState((prev) => ({ ...prev, ...partial }));

  const toggleAnalyst = (id: string) =>
    setState((prev) => ({
      ...prev,
      analystIds: prev.analystIds.includes(id)
        ? prev.analystIds.filter((a) => a !== id)
        : [...prev.analystIds, id],
    }));

  const togglePeerDim = (dim: (typeof PEER_GROUP_DIMS)[number]) =>
    setState((prev) => ({
      ...prev,
      peerGroupDims: prev.peerGroupDims.includes(dim)
        ? prev.peerGroupDims.filter((d) => d !== dim)
        : [...prev.peerGroupDims, dim],
    }));

  const dynamicSelection = state.selection === "top_n" || state.selection === "top_quartile";

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const errors = validateBacktestForm(state);
    if (errors.length > 0) {
      setClientErrors(errors);
      return;
    }
    setClientErrors([]);
    submit.mutate(buildBacktestRequest(state) as Parameters<typeof submit.mutate>[0], {
      onSuccess: (result) => onSubmitted?.(result),
    });
  };

  return (
    <form
      onSubmit={onSubmit}
      className="flex flex-col gap-4 border border-(--color-divider) bg-(--color-surface) p-4"
      data-testid="backtest-builder"
    >
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name (optional)">
          <input
            className="input"
            value={state.name}
            onChange={(e) => patch({ name: e.target.value })}
            aria-label="Backtest name"
          />
        </Field>
      </div>

      <Section title="Signal">
        <div className="grid grid-cols-3 gap-3">
          <Field label="Selection">
            <select
              className="input"
              value={state.selection}
              aria-label="Selection"
              onChange={(e) =>
                patch({ selection: e.target.value as BacktestFormState["selection"] })
              }
            >
              {SELECTION_MODES.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Score field">
            <select
              className="input"
              value={state.scoreField}
              aria-label="Score field"
              onChange={(e) =>
                patch({ scoreField: e.target.value as BacktestFormState["scoreField"] })
              }
            >
              {SCORE_FIELDS.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          </Field>
          {state.selection === "top_n" && (
            <Field label="Top N" hint="Required for top_n">
              <NumInput
                ariaLabel="Top N"
                value={state.topN}
                onChange={(v) => patch({ topN: v })}
                step="1"
              />
            </Field>
          )}
          {dynamicSelection && (
            <Field
              label="Score lookback (sessions)"
              hint="REQUIRED for dynamic selection — trailing ranking window"
            >
              <NumInput
                ariaLabel="Score lookback sessions"
                value={state.scoreLookback}
                onChange={(v) => patch({ scoreLookback: v })}
                step="1"
              />
            </Field>
          )}
          {state.selection === "peer_group" && (
            <>
              <Field label="Peer group" hint='Format: "dim=value|..."'>
                {peerGroupOptions.length > 0 ? (
                  <select
                    className="input"
                    value={state.peerGroup}
                    aria-label="Peer group"
                    onChange={(e) => patch({ peerGroup: e.target.value })}
                  >
                    <option value="">Select…</option>
                    {peerGroupOptions.map((g) => (
                      <option key={g} value={g}>
                        {g}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    className="input"
                    value={state.peerGroup}
                    aria-label="Peer group"
                    onChange={(e) => patch({ peerGroup: e.target.value })}
                    placeholder="cap_bucket=large|region=US|sector=tech"
                  />
                )}
              </Field>
              <Field label="Peer group dims">
                <span className="flex flex-wrap gap-3">
                  {PEER_GROUP_DIMS.map((dim) => (
                    <label key={dim} className="flex items-center gap-1 text-xs">
                      <input
                        type="checkbox"
                        checked={state.peerGroupDims.includes(dim)}
                        onChange={() => togglePeerDim(dim)}
                      />
                      {dim}
                    </label>
                  ))}
                </span>
              </Field>
            </>
          )}
        </div>

        <div className="flex flex-col gap-1">
          <span className="text-xs text-(--color-neutral-600)">
            Analyst filter (optional — restricts the signal pool for any selection)
          </span>
          {isDefaulting || analysts.isPending ? (
            <p className="text-xs text-(--color-neutral-500)">Loading analysts…</p>
          ) : analysts.isError ? (
            <p className="text-xs text-(--color-neutral-500)">
              Analyst list unavailable — submit without a filter or retry later.
            </p>
          ) : analystItems.length === 0 ? (
            <p className="text-xs text-(--color-neutral-500)">No analysts in the active run.</p>
          ) : (
            <fieldset className="grid max-h-40 grid-cols-2 gap-1 overflow-y-auto border border-(--color-divider) p-2">
              <legend className="sr-only">Analyst filter</legend>
              {analystItems.map((a) => (
                <label key={a.analyst_id} className="flex items-center gap-1 text-xs">
                  <input
                    type="checkbox"
                    checked={state.analystIds.includes(a.analyst_id)}
                    onChange={() => toggleAnalyst(a.analyst_id)}
                  />
                  <span>
                    {a.name ?? a.analyst_id}{" "}
                    <span className="text-(--color-neutral-500)">{a.analyst_id}</span>
                  </span>
                </label>
              ))}
            </fieldset>
          )}
        </div>
      </Section>

      <Section title="Portfolio">
        <div className="grid grid-cols-3 gap-3">
          <Field label="Sizing">
            <select
              className="input"
              value={state.sizing}
              aria-label="Sizing"
              onChange={(e) => patch({ sizing: e.target.value as BacktestFormState["sizing"] })}
            >
              {SIZING_MODES.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Mode">
            <select
              className="input"
              value={state.mode}
              aria-label="Mode"
              onChange={(e) => patch({ mode: e.target.value as BacktestFormState["mode"] })}
            >
              {PORTFOLIO_MODES.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Gross exposure cap" hint="(0, 2]; long_only <= 1">
            <NumInput
              ariaLabel="Gross exposure cap"
              value={state.grossExposureCap}
              onChange={(v) => patch({ grossExposureCap: v })}
              step="0.05"
            />
          </Field>
          <Field label="Max position weight" hint="(0, 1]">
            <NumInput
              ariaLabel="Max position weight"
              value={state.maxPositionWeight}
              onChange={(v) => patch({ maxPositionWeight: v })}
              step="0.05"
            />
          </Field>
          <Field label="Liquidity cap" hint="Optional; max weight as fraction of adv_20d">
            <NumInput
              ariaLabel="Liquidity cap"
              value={state.liquidityCap}
              onChange={(v) => patch({ liquidityCap: v })}
              step="0.01"
              placeholder="off"
            />
          </Field>
          <Field label="Vol lookback (sessions)">
            <NumInput
              ariaLabel="Volatility lookback sessions"
              value={state.volLookback}
              onChange={(v) => patch({ volLookback: v })}
              step="1"
            />
          </Field>
        </div>
        <label className="flex items-center gap-2 text-xs text-(--color-neutral-700)">
          <input
            type="checkbox"
            checked={state.allowShorting}
            onChange={(e) => patch({ allowShorting: e.target.checked })}
          />
          Allow shorting (required for long_short / market_neutral / beta_neutral)
        </label>

        <div className="flex flex-col gap-1">
          <span className="text-xs text-(--color-neutral-600)">Sector caps (optional)</span>
          {state.sectorCaps.map((row, i) => (
            <div key={row.uid || `cap-${i}`} className="flex items-center gap-2">
              <select
                className="input flex-1"
                value={row.sectorId}
                aria-label={`Sector cap ${i + 1} sector`}
                onChange={(e) =>
                  patch({
                    sectorCaps: state.sectorCaps.map((r, j) =>
                      j === i ? { ...r, sectorId: e.target.value } : r,
                    ),
                  })
                }
              >
                <option value="">Select sector…</option>
                {sectorOptions.map((s) => (
                  <option key={s.sector_id} value={s.sector_id}>
                    {s.name} ({s.sector_id})
                  </option>
                ))}
              </select>
              <NumInput
                ariaLabel={`Sector cap ${i + 1} max weight`}
                value={row.weight}
                onChange={(v) =>
                  patch({
                    sectorCaps: state.sectorCaps.map((r, j) => (j === i ? { ...r, weight: v } : r)),
                  })
                }
                step="0.05"
                placeholder="max |w|"
              />
              <button
                type="button"
                className="btn btn-ghost"
                aria-label={`Remove sector cap ${i + 1}`}
                onClick={() => patch({ sectorCaps: state.sectorCaps.filter((_, j) => j !== i) })}
              >
                ×
              </button>
            </div>
          ))}
          <div>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() =>
                patch({
                  sectorCaps: [
                    ...state.sectorCaps,
                    { uid: crypto.randomUUID(), sectorId: "", weight: "" },
                  ],
                })
              }
            >
              Add sector cap
            </button>
          </div>
        </div>
      </Section>

      <Section title="Schedule">
        <div className="grid grid-cols-3 gap-3">
          <Field label="Rebalance">
            <select
              className="input"
              value={state.rebalance}
              aria-label="Rebalance"
              onChange={(e) =>
                patch({ rebalance: e.target.value as BacktestFormState["rebalance"] })
              }
            >
              {REBALANCE_FREQS.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Horizon">
            <select
              className="input"
              value={state.horizon}
              aria-label="Horizon"
              onChange={(e) => patch({ horizon: e.target.value as BacktestFormState["horizon"] })}
            >
              {HORIZON_MODES.map((h) => (
                <option key={h} value={h}>
                  {h}
                </option>
              ))}
            </select>
          </Field>
          {state.horizon === "fixed_days" && (
            <Field label="Horizon days" hint="Required for fixed_days">
              <NumInput
                ariaLabel="Horizon days"
                value={state.horizonDays}
                onChange={(v) => patch({ horizonDays: v })}
                step="1"
              />
            </Field>
          )}
        </div>
      </Section>

      <Section title="Costs">
        <div className="grid grid-cols-3 gap-3">
          <Field label="Transaction cost (bps)">
            <NumInput
              ariaLabel="Transaction cost bps"
              value={state.transactionCostBps}
              onChange={(v) => patch({ transactionCostBps: v })}
              step="0.1"
            />
          </Field>
          <Field label="Slippage (bps)" hint="Optional extra cost">
            <NumInput
              ariaLabel="Slippage bps"
              value={state.slippageBps}
              onChange={(v) => patch({ slippageBps: v })}
              step="0.1"
              placeholder="off"
            />
          </Field>
          <Field label="Risk-free annual" hint="For Sharpe; optional">
            <NumInput
              ariaLabel="Risk-free annual"
              value={state.riskFreeAnnual}
              onChange={(v) => patch({ riskFreeAnnual: v })}
              step="0.01"
              placeholder="0"
            />
          </Field>
        </div>
      </Section>

      <Section title="Dates & benchmark">
        <div className="grid grid-cols-3 gap-3">
          <Field label="Start date" hint="Optional; earlier sessions only warm up">
            <input
              className="input"
              type="date"
              value={state.startDate}
              aria-label="Start date"
              onChange={(e) => patch({ startDate: e.target.value })}
            />
          </Field>
          <Field label="End date" hint="Optional; bounds the NAV series">
            <input
              className="input"
              type="date"
              value={state.endDate}
              aria-label="End date"
              onChange={(e) => patch({ endDate: e.target.value })}
            />
          </Field>
          <Field label="Benchmark" hint="Custom benchmarks must be materialized (ready)">
            <select
              className="input"
              value={state.benchmarkId}
              aria-label="Benchmark"
              onChange={(e) => patch({ benchmarkId: e.target.value })}
            >
              <option value="">Market benchmark (default)</option>
              {readyBenchmarks.map((b) => (
                <option key={b.benchmark_id} value={b.benchmark_id}>
                  {b.name ?? b.benchmark_id} ({b.benchmark_id})
                </option>
              ))}
            </select>
          </Field>
        </div>
      </Section>

      {clientErrors.length > 0 && (
        <ul className="list-disc pl-5 text-sm text-(--color-neutral-700)" role="alert">
          {clientErrors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      {submit.isError && (
        <p className="text-sm text-(--color-neutral-700)" role="alert" data-testid="backtest-error">
          {formatApiError(submit.error)}
        </p>
      )}

      <div className="flex gap-2">
        <button type="submit" className="btn btn-primary" disabled={submit.isPending}>
          {submit.isPending ? "Submitting…" : "Submit backtest"}
        </button>
        {onCancel && (
          <button type="button" className="btn btn-secondary" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
