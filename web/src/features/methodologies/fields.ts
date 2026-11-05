import type { Schemas } from "../../api/query";

export type Methodology = Schemas["Methodology"];
export type JsonObject = Record<string, unknown>;

export const ENTRY_CONVENTIONS = ["next_open", "same_close", "t_plus_2_open"] as const;
export const EXPIRY_POLICIES = ["hard_close", "carry_forward", "decay"] as const;
export const BENCHMARK_MODES = ["market", "sector", "beta_adjusted", "custom"] as const;
export const SCORING_MODES = ["absolute", "relative_to_consensus"] as const;
export const RETURN_BASES = ["total", "price"] as const;
export const SHRINKAGE_METHODS = ["none", "empirical_bayes"] as const;
export const PEER_DIMENSIONS = ["sector", "region", "cap_bucket"] as const;
export const POSITION_WEIGHT_KEYS = ["strong_buy", "buy", "hold", "sell", "strong_sell"] as const;
export const FAMILY_WEIGHT_KEYS = [
  "return_skill",
  "timing",
  "price_target",
  "behavior",
  "consistency",
] as const;

export type FieldKind =
  | "int"
  | "number"
  | "boolean"
  | "string"
  | "enum"
  | "enum_list"
  | "number_list"
  | "dict_number";

export interface FieldSpec {
  /** Dot path into the methodology object (dict fields use the group path). */
  path: string;
  section: string;
  label: string;
  kind: FieldKind;
  options?: readonly string[];
  /** Sub-keys rendered as individual numeric inputs (kind "dict_number"). */
  keys?: readonly string[];
  /** Empty string maps to JSON null (strings only). */
  nullable?: boolean;
  /** Server-fixed constants — rendered read-only. */
  readOnly?: boolean;
  hint?: string;
}

interface SectionSpec {
  name: string;
  fields: FieldSpec[];
}

const SECTIONS: SectionSpec[] = [
  {
    name: "Entry & expiry",
    fields: [
      {
        path: "entry_convention",
        section: "Entry & expiry",
        label: "Entry convention",
        kind: "enum",
        options: ENTRY_CONVENTIONS,
      },
      {
        path: "expiry_policy",
        section: "Entry & expiry",
        label: "Expiry policy",
        kind: "enum",
        options: EXPIRY_POLICIES,
      },
      { path: "decay_days", section: "Entry & expiry", label: "Decay days", kind: "int" },
      {
        path: "include_announcement_window",
        section: "Entry & expiry",
        label: "Include announcement window",
        kind: "boolean",
      },
    ],
  },
  {
    name: "Benchmark & scoring",
    fields: [
      {
        path: "benchmark_mode",
        section: "Benchmark & scoring",
        label: "Benchmark mode",
        kind: "enum",
        options: BENCHMARK_MODES,
      },
      {
        path: "benchmark_id",
        section: "Benchmark & scoring",
        label: "Benchmark id",
        kind: "string",
        nullable: true,
        hint: "Empty = benchmark resolved from benchmark_mode",
      },
      {
        path: "scoring_mode",
        section: "Benchmark & scoring",
        label: "Scoring mode",
        kind: "enum",
        options: SCORING_MODES,
      },
    ],
  },
  {
    name: "Position weights",
    fields: [
      {
        path: "position_weights",
        section: "Position weights",
        label: "Position weights",
        kind: "dict_number",
        keys: POSITION_WEIGHT_KEYS,
      },
    ],
  },
  {
    name: "Horizons & eligibility",
    fields: [
      {
        path: "fixed_horizons_days",
        section: "Horizons & eligibility",
        label: "Fixed horizons (days)",
        kind: "number_list",
        hint: "Comma-separated day counts",
      },
      {
        path: "primary_fixed_horizon_days",
        section: "Horizons & eligibility",
        label: "Primary fixed horizon (days)",
        kind: "int",
      },
      {
        path: "min_consensus_ratings",
        section: "Horizons & eligibility",
        label: "Min consensus ratings",
        kind: "int",
      },
      {
        path: "min_ratings_for_rank",
        section: "Horizons & eligibility",
        label: "Min ratings for rank",
        kind: "int",
      },
    ],
  },
  {
    name: "Peer grouping",
    fields: [
      {
        path: "shrinkage_method",
        section: "Peer grouping",
        label: "Shrinkage method",
        kind: "enum",
        options: SHRINKAGE_METHODS,
      },
      {
        path: "peer_group_dims",
        section: "Peer grouping",
        label: "Peer group dimensions",
        kind: "enum_list",
        options: PEER_DIMENSIONS,
        hint: "Comma-separated: sector, region, cap_bucket",
      },
      { path: "min_peer_size", section: "Peer grouping", label: "Min peer size", kind: "int" },
      {
        path: "peer_fallback",
        section: "Peer grouping",
        label: "Peer fallback",
        kind: "string",
        readOnly: true,
      },
    ],
  },
  {
    name: "Costs",
    fields: [
      {
        path: "transaction_cost_bps",
        section: "Costs",
        label: "Transaction cost (bps)",
        kind: "number",
      },
    ],
  },
  {
    name: "Composite",
    fields: [
      {
        path: "return_basis",
        section: "Composite",
        label: "Return basis",
        kind: "enum",
        options: RETURN_BASES,
      },
      {
        path: "family_weights",
        section: "Composite",
        label: "Family weights",
        kind: "dict_number",
        keys: FAMILY_WEIGHT_KEYS,
      },
    ],
  },
  {
    name: "Bootstrap & inference",
    fields: [
      {
        path: "bootstrap_draws",
        section: "Bootstrap & inference",
        label: "Bootstrap draws",
        kind: "int",
      },
      {
        path: "bootstrap_seed",
        section: "Bootstrap & inference",
        label: "Bootstrap seed",
        kind: "int",
      },
      {
        path: "confidence_level",
        section: "Bootstrap & inference",
        label: "Confidence level",
        kind: "number",
      },
    ],
  },
  {
    name: "Reference",
    fields: [
      {
        path: "contract_version",
        section: "Reference",
        label: "Contract version",
        kind: "int",
        readOnly: true,
      },
      {
        path: "reporting_currency",
        section: "Reference",
        label: "Reporting currency",
        kind: "string",
      },
      {
        path: "missing_fx_policy",
        section: "Reference",
        label: "Missing FX policy",
        kind: "string",
        readOnly: true,
      },
    ],
  },
];

function humanize(key: string): string {
  return key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

/**
 * Editable leaf fields: `dict_number` groups expand to one entry per key
 * (`position_weights.buy`), everything else stays a single leaf.
 */
export interface LeafField {
  path: string;
  section: string;
  label: string;
  kind: Exclude<FieldKind, "dict_number">;
  options?: readonly string[];
  nullable?: boolean;
  readOnly?: boolean;
  hint?: string;
}

export const LEAF_FIELDS: LeafField[] = SECTIONS.flatMap((section) =>
  section.fields.flatMap((spec): LeafField[] => {
    if (spec.kind === "dict_number" && spec.keys) {
      return spec.keys.map((key) => ({
        path: `${spec.path}.${key}`,
        section: spec.section,
        label: `${spec.label}: ${humanize(key)}`,
        kind: "number" as const,
        readOnly: spec.readOnly,
      }));
    }
    const { keys: _keys, ...rest } = spec;
    return [{ ...rest, kind: spec.kind as Exclude<FieldKind, "dict_number"> }];
  }),
);

export const SEED_LEAF_FIELDS = LEAF_FIELDS.filter((f) => !f.readOnly);

export function sections(): { name: string; fields: LeafField[] }[] {
  const out: { name: string; fields: LeafField[] }[] = [];
  for (const name of SECTIONS.map((s) => s.name)) {
    const fields = LEAF_FIELDS.filter((f) => f.section === name);
    if (fields.length > 0) out.push({ name, fields });
  }
  return out;
}

// ---------------------------------------------------------------------------
// Path helpers
// ---------------------------------------------------------------------------

export function isPlainObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function getPathValue(obj: unknown, path: string): unknown {
  let cur: unknown = obj;
  for (const part of path.split(".")) {
    if (!isPlainObject(cur)) return undefined;
    cur = cur[part];
  }
  return cur;
}

/** Return a deep clone of `obj` with `path` set to `value`. */
export function setPathValue<T>(obj: T, path: string, value: unknown): T {
  const parts = path.split(".");
  const clone: unknown = Array.isArray(obj) ? [...obj] : isPlainObject(obj) ? { ...obj } : {};
  let cur = clone as JsonObject;
  for (let i = 0; i < parts.length - 1; i++) {
    const part = parts[i];
    const next = cur[part];
    const nextClone: JsonObject = isPlainObject(next) ? { ...next } : {};
    cur[part] = nextClone;
    cur = nextClone;
  }
  cur[parts[parts.length - 1]] = value;
  return clone as T;
}

/** Deep-merge `overlay` onto `base` (plain objects merge recursively). */
export function mergeMethodology(
  base: JsonObject,
  overlay: JsonObject | null | undefined,
): JsonObject {
  const out: JsonObject = { ...base };
  for (const [key, value] of Object.entries(overlay ?? {})) {
    if (isPlainObject(value) && isPlainObject(out[key])) {
      out[key] = mergeMethodology(out[key] as JsonObject, value);
    } else if (value !== undefined) {
      out[key] = value;
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// String <-> typed conversion (form state is Record<path, string>)
// ---------------------------------------------------------------------------

function formatScalar(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (Array.isArray(value)) {
    return value.map((v) => String(v)).join(", ");
  }
  if (isPlainObject(value)) {
    return Object.entries(value)
      .map(([k, v]) => `${k}=${String(v)}`)
      .join(", ");
  }
  return String(value);
}

/** Serialize a methodology (or partial) into per-leaf string form (read-only
 *  constants included so they can be displayed). */
export function methodologyToStrings(methodology: JsonObject): Record<string, string> {
  const out: Record<string, string> = {};
  for (const field of LEAF_FIELDS) {
    out[field.path] = formatScalar(getPathValue(methodology, field.path));
  }
  return out;
}

export type CoerceResult = { ok: true; value: unknown } | { ok: false; error: string };

function parseNumberList(raw: string): number[] | null {
  const parts = raw
    .split(",")
    .map((p) => p.trim())
    .filter((p) => p.length > 0);
  const nums: number[] = [];
  for (const p of parts) {
    const n = Number(p);
    if (!Number.isFinite(n)) return null;
    nums.push(n);
  }
  return nums;
}

/** Coerce one raw form string into the field's JSON value. */
export function coerceFieldValue(field: LeafField, raw: string): CoerceResult {
  const trimmed = raw.trim();
  switch (field.kind) {
    case "int": {
      if (!/^-?\d+$/.test(trimmed)) return { ok: false, error: "must be an integer" };
      return { ok: true, value: Number.parseInt(trimmed, 10) };
    }
    case "number": {
      if (trimmed === "") return { ok: false, error: "required" };
      const n = Number(trimmed);
      if (!Number.isFinite(n)) return { ok: false, error: "must be a number" };
      return { ok: true, value: n };
    }
    case "boolean": {
      if (trimmed === "true") return { ok: true, value: true };
      if (trimmed === "false") return { ok: true, value: false };
      return { ok: false, error: 'must be "true" or "false"' };
    }
    case "string": {
      if (trimmed === "" && field.nullable) return { ok: true, value: null };
      return { ok: true, value: trimmed };
    }
    case "enum": {
      if (field.options && !(field.options as readonly string[]).includes(trimmed)) {
        return { ok: false, error: `must be one of: ${field.options.join(", ")}` };
      }
      return { ok: true, value: trimmed };
    }
    case "enum_list": {
      const parts = trimmed
        .split(",")
        .map((p) => p.trim())
        .filter((p) => p.length > 0);
      const unique = [...new Set(parts)];
      if (field.options) {
        const bad = unique.filter((p) => !(field.options as readonly string[]).includes(p));
        if (bad.length > 0) {
          return { ok: false, error: `unknown values: ${bad.join(", ")}` };
        }
      }
      return { ok: true, value: unique };
    }
    case "number_list": {
      const nums = parseNumberList(trimmed);
      if (nums === null) return { ok: false, error: "must be comma-separated numbers" };
      return { ok: true, value: nums };
    }
  }
}

export interface ApplyResult {
  /** Methodology object with all editable leaves applied; undefined on errors. */
  value?: JsonObject;
  errors: string[];
}

/**
 * Apply string-form leaves onto a base methodology object. Uneditable
 * (read-only/constant) fields are inherited from `base` unchanged.
 */
export function applyStringsToMethodology(
  base: JsonObject,
  strings: Record<string, string>,
): ApplyResult {
  const errors: string[] = [];
  let value: JsonObject = mergeMethodology(base, {});
  for (const field of SEED_LEAF_FIELDS) {
    const raw = strings[field.path] ?? "";
    const result = coerceFieldValue(field, raw);
    if (!result.ok) {
      errors.push(`${field.label}: ${result.error}`);
      continue;
    }
    value = setPathValue(value, field.path, result.value);
  }
  return errors.length > 0 ? { errors } : { value, errors };
}

/** Client-side validation of string-form leaves (labels prefixed). */
export function validateStrings(strings: Record<string, string>): string[] {
  const errors: string[] = [];
  for (const field of SEED_LEAF_FIELDS) {
    const result = coerceFieldValue(field, strings[field.path] ?? "");
    if (!result.ok) errors.push(`${field.label}: ${result.error}`);
  }
  return errors;
}

// ---------------------------------------------------------------------------
// Leaf diffing (methodology overrides / methodology diff display)
// ---------------------------------------------------------------------------

export interface LeafChange {
  path: string;
  a: unknown;
  b: unknown;
  a_missing: boolean;
  b_missing: boolean;
}

function leafEntries(value: unknown, prefix = ""): [string, unknown][] {
  if (isPlainObject(value)) {
    return Object.entries(value).flatMap(([k, v]) => leafEntries(v, prefix ? `${prefix}.${k}` : k));
  }
  return [[prefix, value]];
}

function leafEqual(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) || Array.isArray(b)) {
    return JSON.stringify(a) === JSON.stringify(b);
  }
  return Object.is(a, b);
}

/** Sorted leaf-level diff between two methodology-ish objects. */
export function diffLeaves(a: JsonObject, b: JsonObject): LeafChange[] {
  const mapA = new Map(leafEntries(a));
  const mapB = new Map(leafEntries(b));
  const paths = [...new Set([...mapA.keys(), ...mapB.keys()])].sort();
  const changes: LeafChange[] = [];
  for (const path of paths) {
    const hasA = mapA.has(path);
    const hasB = mapB.has(path);
    const va = mapA.get(path);
    const vb = mapB.get(path);
    if (hasA && hasB && leafEqual(va, vb)) continue;
    changes.push({
      path,
      a: va,
      b: vb,
      a_missing: !hasA,
      b_missing: !hasB,
    });
  }
  return changes;
}

/**
 * Build a partial-overrides object (as accepted by ``POST /api/scenarios``)
 * capturing every leaf where `current` differs from `base`.
 */
export function buildOverrides(base: JsonObject, current: JsonObject): JsonObject {
  const overrides: JsonObject = {};
  for (const change of diffLeaves(base, current)) {
    if (change.b_missing) continue; // removals are not expressible as overrides
    let target = overrides;
    const parts = change.path.split(".");
    for (let i = 0; i < parts.length - 1; i++) {
      const part = parts[i];
      if (!isPlainObject(target[part])) target[part] = {};
      target = target[part] as JsonObject;
    }
    target[parts[parts.length - 1]] = change.b;
  }
  return overrides;
}

/** Human-readable one-line summary of an overrides object. */
export function summarizeOverrides(overrides: JsonObject | null | undefined): string {
  if (!overrides || Object.keys(overrides).length === 0) return "no overrides";
  const parts: string[] = [];
  for (const [key, value] of Object.entries(overrides)) {
    if (isPlainObject(value)) {
      for (const [k, v] of Object.entries(value)) parts.push(`${key}.${k}=${String(v)}`);
    } else {
      parts.push(`${key}=${String(value)}`);
    }
  }
  return parts.join(", ");
}

/** Format a leaf value for read-only display. */
export function formatLeafValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value)) return value.map((v) => String(v)).join(", ");
  if (isPlainObject(value)) {
    return Object.entries(value)
      .map(([k, v]) => `${k}: ${String(v)}`)
      .join(", ");
  }
  return String(value);
}
