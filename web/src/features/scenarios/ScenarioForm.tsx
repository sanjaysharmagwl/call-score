import { type FormEvent, useEffect, useMemo, useState } from "react";
import { shortFingerprint } from "../../api/query";
import { ErrorState } from "../../components/states";
import {
  formatApiError,
  useMethodologyDefaults,
  useMethodologyVersion,
} from "../methodologies/api";
import {
  applyStringsToMethodology,
  buildOverrides,
  type JsonObject,
  mergeMethodology,
  methodologyToStrings,
  summarizeOverrides,
  validateStrings,
} from "../methodologies/fields";
import { MethodologyParamForm } from "../methodologies/MethodologyParamForm";
import { useMethodologies } from "../shared/queries";
import { type ScenarioDetail, useCreateScenario } from "./api";

function parseUniverseFilter(raw: string): string[] | null {
  const parts = raw
    .split(",")
    .map((p) => p.trim())
    .filter((p) => p.length > 0);
  return parts.length > 0 ? parts : null;
}

/**
 * Create-scenario form: name + base methodology (built-in default or a
 * stored fingerprint) + overrides edited through the shared schema-driven
 * parameter form. Only leaves that differ from the base are submitted as
 * `overrides`.
 */
export function ScenarioForm({
  onCreated,
  onCancel,
}: {
  onCreated?: (scenario: ScenarioDetail) => void;
  onCancel?: () => void;
}) {
  const methodologies = useMethodologies();
  const defaults = useMethodologyDefaults();
  const create = useCreateScenario();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [base, setBase] = useState<string>("default");
  const [universeRaw, setUniverseRaw] = useState("");
  const [strings, setStrings] = useState<Record<string, string>>({});
  const [stringsFor, setStringsFor] = useState<JsonObject | null>(null);
  const [clientErrors, setClientErrors] = useState<string[]>([]);

  const isFingerprint = base !== "default";
  const versionQuery = useMethodologyVersion(isFingerprint ? base : undefined);
  const versions = methodologies.data?.data ?? [];

  const baseParams = useMemo<JsonObject | undefined>(() => {
    const preset = (defaults.data?.methodology ?? {}) as JsonObject;
    if (!defaults.data) return undefined;
    if (!isFingerprint) return preset;
    const stored = (versionQuery.data?.params ?? null) as JsonObject | null;
    return mergeMethodology(preset, stored);
  }, [defaults.data, isFingerprint, versionQuery.data]);

  useEffect(() => {
    if (baseParams && baseParams !== stringsFor) {
      setStrings(methodologyToStrings(baseParams));
      setStringsFor(baseParams);
      setClientErrors([]);
    }
  }, [baseParams, stringsFor]);

  const overridesPreview = useMemo(() => {
    if (!baseParams || stringsFor !== baseParams) return {};
    const applied = applyStringsToMethodology(baseParams, strings);
    return applied.value ? buildOverrides(baseParams, applied.value) : {};
  }, [baseParams, strings, stringsFor]);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (!baseParams || !name.trim()) return;
    const errors = validateStrings(strings);
    if (errors.length > 0) {
      setClientErrors(errors);
      return;
    }
    const applied = applyStringsToMethodology(baseParams, strings);
    if (!applied.value) {
      setClientErrors(applied.errors);
      return;
    }
    const overrides = buildOverrides(baseParams, applied.value);
    create.mutate(
      {
        name: name.trim(),
        description: description.trim() || null,
        base_methodology: isFingerprint ? base : "default",
        overrides: Object.keys(overrides).length > 0 ? overrides : {},
        universe_filter: parseUniverseFilter(universeRaw),
      },
      { onSuccess: (scenario) => onCreated?.(scenario) },
    );
  };

  return (
    <form
      onSubmit={onSubmit}
      className="flex flex-col gap-3 border border-(--color-divider) bg-(--color-surface) p-4"
      data-testid="scenario-form"
    >
      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Name
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            aria-label="Scenario name"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Base methodology
          <select
            className="input"
            value={base}
            onChange={(e) => setBase(e.target.value)}
            aria-label="Base methodology"
          >
            <option value="default">Built-in defaults</option>
            {versions.map((v) => (
              <option key={v.fingerprint} value={v.fingerprint}>
                {shortFingerprint(v.fingerprint)}
                {v.is_baseline ? " · baseline" : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Description
          <input
            className="input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Universe filter (optional, comma-separated security ids)
          <input
            className="input"
            value={universeRaw}
            onChange={(e) => setUniverseRaw(e.target.value)}
            placeholder="SEC_1, SEC_2"
          />
        </label>
      </div>

      {isFingerprint && versionQuery.isPending && (
        <p className="text-sm text-(--color-neutral-600)">Loading base parameters…</p>
      )}
      {isFingerprint && versionQuery.isError && (
        <ErrorState error={versionQuery.error} title="Failed to load base methodology" />
      )}

      {baseParams && stringsFor === baseParams && (
        <div className="flex flex-col gap-2">
          <p className="text-xs text-(--color-neutral-600)">
            Overrides — edit any parameter below; only changed leaves are stored. Current:{" "}
            <code className="text-[11px]" data-testid="overrides-preview">
              {summarizeOverrides(overridesPreview)}
            </code>
          </p>
          <MethodologyParamForm
            values={strings}
            baseline={methodologyToStrings(baseParams)}
            onFieldChange={(path, v) => setStrings((prev) => ({ ...prev, [path]: v }))}
          />
        </div>
      )}

      {clientErrors.length > 0 && (
        <ul className="list-disc pl-5 text-sm text-(--color-neutral-700)" role="alert">
          {clientErrors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      {create.isError && (
        <p className="text-sm text-(--color-neutral-700)" role="alert" data-testid="scenario-error">
          {formatApiError(create.error)}
        </p>
      )}

      <div className="flex gap-2">
        <button
          type="submit"
          className="btn btn-primary"
          disabled={create.isPending || !baseParams || !name.trim()}
        >
          {create.isPending ? "Creating…" : "Create scenario"}
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
