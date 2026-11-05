import { describe, expect, it } from "vitest";
import {
  applyStringsToMethodology,
  buildOverrides,
  coerceFieldValue,
  diffLeaves,
  type LeafField,
  mergeMethodology,
  methodologyToStrings,
  SEED_LEAF_FIELDS,
  summarizeOverrides,
  validateStrings,
} from "./fields";
import { methodologyFixture } from "./test-utils";

const numField: LeafField = {
  path: "decay_days",
  section: "Entry & expiry",
  label: "Decay days",
  kind: "int",
};

describe("coerceFieldValue", () => {
  it("parses ints, numbers, booleans, enums and lists", () => {
    expect(coerceFieldValue(numField, "120")).toEqual({ ok: true, value: 120 });
    expect(coerceFieldValue(numField, "1.5")).toEqual({ ok: false, error: "must be an integer" });
    expect(coerceFieldValue({ ...numField, kind: "number" }, "0.05")).toEqual({
      ok: true,
      value: 0.05,
    });
    expect(
      coerceFieldValue({ path: "x", section: "s", label: "x", kind: "boolean" }, "true"),
    ).toEqual({ ok: true, value: true });
    expect(
      coerceFieldValue(
        {
          path: "peer_group_dims",
          section: "s",
          label: "dims",
          kind: "enum_list",
          options: ["sector", "region", "cap_bucket"],
        },
        "sector, bogus",
      ),
    ).toEqual({ ok: false, error: "unknown values: bogus" });
    expect(
      coerceFieldValue({ path: "h", section: "s", label: "h", kind: "number_list" }, "63, 126,252"),
    ).toEqual({ ok: true, value: [63, 126, 252] });
  });
});

describe("methodologyToStrings / applyStringsToMethodology", () => {
  it("round-trips a full methodology with no diffs", () => {
    const base = methodologyFixture();
    const strings = methodologyToStrings(base);
    const applied = applyStringsToMethodology(base, strings);
    expect(applied.errors).toEqual([]);
    expect(buildOverrides(base, applied.value ?? {})).toEqual({});
  });

  it("applies changed leaves and produces nested overrides", () => {
    const base = methodologyFixture();
    const strings = methodologyToStrings(base);
    strings.decay_days = "45";
    strings["family_weights.return_skill"] = "0.6";
    strings["family_weights.timing"] = "0.1";
    const applied = applyStringsToMethodology(base, strings);
    expect(applied.errors).toEqual([]);
    expect(applied.value?.decay_days).toBe(45);
    const fw = applied.value?.family_weights as Record<string, number> | undefined;
    expect(fw?.return_skill).toBe(0.6);
    const overrides = buildOverrides(base, applied.value ?? {});
    expect(overrides).toEqual({
      decay_days: 45,
      family_weights: { return_skill: 0.6, timing: 0.1 },
    });
  });

  it("collects validation errors with labels", () => {
    const base = methodologyFixture();
    const strings = methodologyToStrings(base);
    strings.decay_days = "abc";
    strings.confidence_level = "";
    const errors = validateStrings(strings);
    expect(errors.some((e) => e.includes("Decay days"))).toBe(true);
    expect(errors.some((e) => e.includes("Confidence level"))).toBe(true);
    const applied = applyStringsToMethodology(base, strings);
    expect(applied.value).toBeUndefined();
    expect(applied.errors.length).toBe(2);
  });

  it("keeps read-only constants from the base", () => {
    const base = methodologyFixture();
    const strings = methodologyToStrings(base);
    // contract_version / peer_fallback / missing_fx_policy are not editable.
    expect(SEED_LEAF_FIELDS.some((f) => f.path === "contract_version")).toBe(false);
    const applied = applyStringsToMethodology(base, strings);
    expect(applied.value?.contract_version).toBe(1);
    expect(applied.value?.peer_fallback).toBe("unranked");
    expect(applied.value?.missing_fx_policy).toBe("fail");
  });
});

describe("diffLeaves / mergeMethodology", () => {
  it("reports leaf changes sorted by path", () => {
    const a = methodologyFixture();
    const b = methodologyFixture({ decay_days: 30, transaction_cost_bps: 10 });
    const changes = diffLeaves(a, b);
    expect(changes.map((c) => c.path)).toEqual(["decay_days", "transaction_cost_bps"]);
    expect(changes[0]).toMatchObject({ a: 90, b: 30 });
  });

  it("deep-merges overlay onto base", () => {
    const base = methodologyFixture();
    const merged = mergeMethodology(base, {
      family_weights: { return_skill: 0.9 },
      decay_days: 10,
    });
    expect(merged.decay_days).toBe(10);
    expect((merged.family_weights as Record<string, number>).return_skill).toBe(0.9);
    expect((merged.family_weights as Record<string, number>).timing).toBe(0.15);
  });

  it("summarizes overrides for display", () => {
    expect(summarizeOverrides({})).toBe("no overrides");
    expect(
      summarizeOverrides({ transaction_cost_bps: 25, family_weights: { return_skill: 0.6 } }),
    ).toBe("transaction_cost_bps=25, family_weights.return_skill=0.6");
  });
});
