/**
 * Backtest builder form model: string-valued inputs, engine-rule validation,
 * and the `BacktestCreateRequest` payload builder. Mirrors
 * `callscore_engine.backtest.BacktestSpec` / `benchmarks.BenchmarkSpec`
 * validation so most violations are caught client-side; server 422s are
 * still surfaced verbatim by the form.
 */

export type SelectionMode = "all" | "top_n" | "top_quartile" | "peer_group";
export type ScoreField = "position_weight" | "rating_ordinal" | "trailing_return";
export type SizingMode = "equal" | "conviction" | "inverse_vol";
export type PortfolioMode = "long_only" | "long_short" | "market_neutral" | "beta_neutral";
export type RebalanceFreq = "event_driven" | "weekly" | "monthly";
export type HorizonMode = "until_change" | "fixed_days";

export const SELECTION_MODES: SelectionMode[] = ["all", "top_n", "top_quartile", "peer_group"];
export const SCORE_FIELDS: ScoreField[] = ["position_weight", "rating_ordinal", "trailing_return"];
export const SIZING_MODES: SizingMode[] = ["equal", "conviction", "inverse_vol"];
export const PORTFOLIO_MODES: PortfolioMode[] = [
  "long_only",
  "long_short",
  "market_neutral",
  "beta_neutral",
];
export const REBALANCE_FREQS: RebalanceFreq[] = ["event_driven", "weekly", "monthly"];
export const HORIZON_MODES: HorizonMode[] = ["until_change", "fixed_days"];
export const PEER_GROUP_DIMS = ["cap_bucket", "region", "sector"] as const;
export type PeerGroupDim = (typeof PEER_GROUP_DIMS)[number];

const SHORT_MODES = new Set<PortfolioMode>(["long_short", "market_neutral", "beta_neutral"]);
const DYNAMIC_SELECTIONS = new Set<SelectionMode>(["top_n", "top_quartile"]);

export interface SectorCapRow {
  /** Stable client-side key for list rendering (not sent to the API). */
  uid: string;
  sectorId: string;
  /** Raw text input; parsed to a positive number on submit. */
  weight: string;
}

/** All form inputs are strings so controlled inputs stay simple. */
export interface BacktestFormState {
  name: string;
  selection: SelectionMode;
  analystIds: string[];
  topN: string;
  scoreLookback: string;
  scoreField: ScoreField;
  peerGroup: string;
  peerGroupDims: PeerGroupDim[];
  sizing: SizingMode;
  mode: PortfolioMode;
  grossExposureCap: string;
  maxPositionWeight: string;
  sectorCaps: SectorCapRow[];
  liquidityCap: string;
  allowShorting: boolean;
  volLookback: string;
  rebalance: RebalanceFreq;
  horizon: HorizonMode;
  horizonDays: string;
  transactionCostBps: string;
  slippageBps: string;
  riskFreeAnnual: string;
  startDate: string;
  endDate: string;
  /** Selected custom benchmark id; "" = engine default market benchmark. */
  benchmarkId: string;
}

export function defaultBacktestForm(): BacktestFormState {
  return {
    name: "",
    selection: "all",
    analystIds: [],
    topN: "",
    scoreLookback: "",
    scoreField: "position_weight",
    peerGroup: "",
    peerGroupDims: [...PEER_GROUP_DIMS],
    sizing: "equal",
    mode: "long_only",
    grossExposureCap: "1",
    maxPositionWeight: "1",
    sectorCaps: [],
    liquidityCap: "",
    allowShorting: true,
    volLookback: "60",
    rebalance: "event_driven",
    horizon: "until_change",
    horizonDays: "",
    transactionCostBps: "0",
    slippageBps: "",
    riskFreeAnnual: "",
    startDate: "",
    endDate: "",
    benchmarkId: "",
  };
}

function parseNumber(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : Number.NaN;
}

function parsePositiveInt(raw: string): number | null {
  const value = parseNumber(raw);
  if (value === null || !Number.isInteger(value) || value <= 0) return null;
  return value;
}

/**
 * Client-side mirror of `BacktestSpec`'s `coherent` validator plus field
 * bounds. Returns human-readable error strings (empty = valid).
 */
export function validateBacktestForm(state: BacktestFormState): string[] {
  const errors: string[] = [];

  if (DYNAMIC_SELECTIONS.has(state.selection)) {
    if (parsePositiveInt(state.scoreLookback) === null) {
      errors.push(
        `selection='${state.selection}' requires score_lookback_sessions (positive integer)`,
      );
    }
  }
  if (state.selection === "top_n" && parsePositiveInt(state.topN) === null) {
    errors.push("selection='top_n' requires top_n (positive integer)");
  }
  if (state.selection === "peer_group" && state.peerGroup.trim() === "") {
    errors.push("selection='peer_group' requires peer_group");
  }
  if (state.horizon === "fixed_days" && parsePositiveInt(state.horizonDays) === null) {
    errors.push("horizon='fixed_days' requires horizon_days (positive integer)");
  }

  const gross = parseNumber(state.grossExposureCap);
  if (gross === null || Number.isNaN(gross) || gross <= 0 || gross > 2) {
    errors.push("gross_exposure_cap must be in (0, 2]");
  } else if (state.mode === "long_only" && gross > 1) {
    errors.push("long_only mode cannot exceed gross_exposure_cap 1.0");
  }

  const maxPos = parseNumber(state.maxPositionWeight);
  if (maxPos === null || Number.isNaN(maxPos) || maxPos <= 0 || maxPos > 1) {
    errors.push("max_position_weight must be in (0, 1]");
  }

  const volLookback = parsePositiveInt(state.volLookback);
  if (volLookback === null) {
    errors.push("vol_lookback_sessions must be a positive integer");
  }

  for (const [label, raw] of [
    ["transaction_cost_bps", state.transactionCostBps],
    ["slippage_bps", state.slippageBps],
    ["risk_free_annual", state.riskFreeAnnual],
    ["liquidity_cap", state.liquidityCap],
  ] as const) {
    const value = parseNumber(raw);
    if (raw.trim() !== "" && (value === null || Number.isNaN(value) || value < 0)) {
      errors.push(`${label} must be a non-negative number`);
    }
  }
  const cost = parseNumber(state.transactionCostBps);
  if (cost !== null && !Number.isNaN(cost) && cost > 10_000) {
    errors.push("transaction_cost_bps must be <= 10000");
  }
  const slip = parseNumber(state.slippageBps);
  if (slip !== null && !Number.isNaN(slip) && slip > 10_000) {
    errors.push("slippage_bps must be <= 10000");
  }

  if (SHORT_MODES.has(state.mode) && !state.allowShorting) {
    errors.push(`mode='${state.mode}' requires shorting but allow_shorting is off`);
  }

  for (const row of state.sectorCaps) {
    if (row.sectorId.trim() === "" && row.weight.trim() === "") continue;
    const weight = parseNumber(row.weight);
    if (row.sectorId.trim() === "" || weight === null || Number.isNaN(weight) || weight <= 0) {
      errors.push(
        `sector cap row needs a sector id and a positive weight (got '${row.sectorId}' / '${row.weight}')`,
      );
    }
  }

  if (state.startDate && state.endDate && state.startDate > state.endDate) {
    errors.push("start_date must be on or before end_date");
  }

  return errors;
}

export interface BacktestRequestParts {
  name?: string;
  spec: Record<string, unknown>;
  start_date?: string;
  end_date?: string;
  benchmark_id?: string;
}

/**
 * Build the `POST /api/backtests` body. Dates and the custom benchmark id
 * are sent as top-level overrides (the API merges them into the spec);
 * everything else lives in `spec` mirroring `BacktestSpec` field names.
 * Only call after `validateBacktestForm` returns no errors.
 */
export function buildBacktestRequest(state: BacktestFormState): BacktestRequestParts {
  const spec: Record<string, unknown> = {
    selection: state.selection,
    score_field: state.scoreField,
    sizing: state.sizing,
    mode: state.mode,
    gross_exposure_cap: Number(state.grossExposureCap),
    max_position_weight: Number(state.maxPositionWeight),
    allow_shorting: state.allowShorting,
    vol_lookback_sessions: Number(state.volLookback),
    rebalance: state.rebalance,
    horizon: state.horizon,
    transaction_cost_bps: Number(state.transactionCostBps),
  };

  if (state.analystIds.length > 0) spec.analyst_ids = [...state.analystIds];
  if (DYNAMIC_SELECTIONS.has(state.selection)) {
    spec.score_lookback_sessions = Number(state.scoreLookback);
  }
  if (state.selection === "top_n") spec.top_n = Number(state.topN);
  if (state.selection === "peer_group") {
    spec.peer_group = state.peerGroup.trim();
    spec.peer_group_dims = [...state.peerGroupDims];
  }

  const caps: Record<string, number> = {};
  for (const row of state.sectorCaps) {
    const sectorId = row.sectorId.trim();
    const weight = parseNumber(row.weight);
    if (sectorId !== "" && weight !== null && !Number.isNaN(weight)) {
      caps[sectorId] = weight;
    }
  }
  if (Object.keys(caps).length > 0) spec.sector_caps = caps;

  const liquidity = parseNumber(state.liquidityCap);
  if (liquidity !== null && !Number.isNaN(liquidity) && state.liquidityCap.trim() !== "") {
    spec.liquidity_cap = liquidity;
  }
  const slippage = parseNumber(state.slippageBps);
  if (slippage !== null && !Number.isNaN(slippage) && state.slippageBps.trim() !== "") {
    spec.slippage_bps = slippage;
  }
  const riskFree = parseNumber(state.riskFreeAnnual);
  if (riskFree !== null && !Number.isNaN(riskFree) && state.riskFreeAnnual.trim() !== "") {
    spec.risk_free_annual = riskFree;
  }
  if (state.horizon === "fixed_days") spec.horizon_days = Number(state.horizonDays);

  const request: BacktestRequestParts = { spec };
  const name = state.name.trim();
  if (name !== "") request.name = name;
  if (state.startDate !== "") request.start_date = state.startDate;
  if (state.endDate !== "") request.end_date = state.endDate;
  if (state.benchmarkId !== "") request.benchmark_id = state.benchmarkId;
  return request;
}

// ---------------------------------------------------------------------------
// Benchmark create form
// ---------------------------------------------------------------------------

export type BenchmarkPreset = "custom" | "equal_weight_market" | "cap_matched" | "sector_neutral";
export type BenchmarkWeighting = "equal" | "cap_weighted" | "free_float" | "cap_matched";
export type BenchmarkRebalance = "daily" | "monthly" | "quarterly";

export const BENCHMARK_PRESETS: BenchmarkPreset[] = [
  "custom",
  "equal_weight_market",
  "cap_matched",
  "sector_neutral",
];
export const BENCHMARK_WEIGHTINGS: BenchmarkWeighting[] = [
  "equal",
  "cap_weighted",
  "free_float",
  "cap_matched",
];
export const BENCHMARK_REBALANCES: BenchmarkRebalance[] = ["daily", "monthly", "quarterly"];

export interface BenchmarkFormState {
  name: string;
  preset: BenchmarkPreset;
  weighting: BenchmarkWeighting;
  rebalance: BenchmarkRebalance;
  /** Long-leg sector for preset='sector_neutral' (also usable as universe filter). */
  sectorId: string;
  universeSectorId: string;
  capBucket: string;
  region: string;
  securityIds: string;
  includeDelisted: boolean;
  startDate: string;
  endDate: string;
}

export function defaultBenchmarkForm(): BenchmarkFormState {
  return {
    name: "",
    preset: "custom",
    weighting: "equal",
    rebalance: "monthly",
    sectorId: "",
    universeSectorId: "",
    capBucket: "",
    region: "",
    securityIds: "",
    includeDelisted: false,
    startDate: "",
    endDate: "",
  };
}

export function validateBenchmarkForm(state: BenchmarkFormState): string[] {
  const errors: string[] = [];
  if (state.preset === "sector_neutral" && state.sectorId.trim() === "") {
    errors.push("preset='sector_neutral' requires sector_id (long leg)");
  }
  if (state.startDate && state.endDate && state.startDate > state.endDate) {
    errors.push("start_date must be on or before end_date");
  }
  return errors;
}

export interface BenchmarkRequestParts {
  name?: string;
  spec: Record<string, unknown>;
}

/**
 * Build the `POST /api/benchmarks` body. `benchmark_id` is omitted — the API
 * assigns a deterministic `CSTM_<hash>` id. `spec.sector_id` is the
 * sector-neutral long leg; `universe.*` carries the PIT membership filters.
 */
export function buildBenchmarkRequest(state: BenchmarkFormState): BenchmarkRequestParts {
  const universe: Record<string, unknown> = {};
  if (state.universeSectorId.trim() !== "") universe.sector_id = state.universeSectorId.trim();
  if (state.capBucket.trim() !== "") universe.cap_bucket = state.capBucket.trim();
  if (state.region.trim() !== "") universe.region = state.region.trim();
  const secIds = state.securityIds
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
  if (secIds.length > 0) universe.security_ids = secIds;
  universe.include_delisted = state.includeDelisted;

  const spec: Record<string, unknown> = {
    preset: state.preset,
    universe,
    weighting: state.weighting,
    rebalance: state.rebalance,
  };
  if (state.sectorId.trim() !== "") spec.sector_id = state.sectorId.trim();
  if (state.startDate !== "") spec.start_date = state.startDate;
  if (state.endDate !== "") spec.end_date = state.endDate;

  const request: BenchmarkRequestParts = { spec };
  const name = state.name.trim();
  if (name !== "") request.name = name;
  return request;
}
