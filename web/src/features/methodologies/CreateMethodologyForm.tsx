import { type FormEvent, useEffect, useMemo, useState } from "react";
import { ErrorState } from "../../components/states";
import {
  formatApiError,
  type MethodologyDefaultsResponse,
  type MethodologyVersionSummary,
  useCreateMethodology,
  useMethodologyVersion,
} from "./api";
import {
  applyStringsToMethodology,
  type JsonObject,
  mergeMethodology,
  methodologyToStrings,
  validateStrings,
} from "./fields";
import { MethodologyParamForm } from "./MethodologyParamForm";

export interface CreateMethodologyFormProps {
  defaults: MethodologyDefaultsResponse | undefined;
  versions: MethodologyVersionSummary[];
  /** Preselect a stored version as the clone source. */
  initialSource?: string;
  onCancel?: () => void;
  onCreated?: (version: MethodologyVersionSummary) => void;
}

/**
 * Create a new immutable methodology version. The full parameter tree is
 * rendered from the built-in defaults (or a clone of a stored version);
 * every editable leaf is validated client-side and server-side (422s are
 * surfaced verbatim).
 */
export function CreateMethodologyForm({
  defaults,
  versions,
  initialSource,
  onCancel,
  onCreated,
}: CreateMethodologyFormProps) {
  const [source, setSource] = useState<string>(initialSource ?? "default");
  const [isBaseline, setIsBaseline] = useState(false);
  const [strings, setStrings] = useState<Record<string, string>>({});
  const [clientErrors, setClientErrors] = useState<string[]>([]);
  const [stringsFor, setStringsFor] = useState<JsonObject | null>(null);

  const isFingerprint = source !== "default";
  const versionQuery = useMethodologyVersion(isFingerprint ? source : undefined);
  const create = useCreateMethodology();

  const baseParams = useMemo<JsonObject | undefined>(() => {
    const preset = (defaults?.methodology ?? {}) as JsonObject;
    if (!isFingerprint) return defaults ? preset : undefined;
    const stored = (versionQuery.data?.params ?? null) as JsonObject | null;
    return mergeMethodology(preset, stored);
  }, [defaults, isFingerprint, versionQuery.data]);

  // (Re)initialize the string form whenever the effective base changes.
  useEffect(() => {
    if (baseParams && baseParams !== stringsFor) {
      setStrings(methodologyToStrings(baseParams));
      setStringsFor(baseParams);
      setClientErrors([]);
    }
  }, [baseParams, stringsFor]);

  const baselineExists = versions.some((v) => v.is_baseline);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (!baseParams) return;
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
    create.mutate(
      { methodology: applied.value, is_baseline: isBaseline },
      {
        onSuccess: (version) => {
          setClientErrors([]);
          onCreated?.(version);
        },
      },
    );
  };

  return (
    <form
      onSubmit={onSubmit}
      className="flex flex-col gap-3 border border-(--color-divider) bg-(--color-surface) p-4"
      data-testid="create-methodology-form"
    >
      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Start from
          <select
            className="input"
            value={source}
            onChange={(e) => setSource(e.target.value)}
            aria-label="Start from"
          >
            <option value="default">Built-in defaults</option>
            {versions.map((v) => (
              <option key={v.fingerprint} value={v.fingerprint}>
                {v.fingerprint.slice(0, 12)}…{v.is_baseline ? " · baseline" : ""} ·{" "}
                {v.created_at.slice(0, 10)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-end gap-2 text-xs text-(--color-neutral-600)">
          <input
            type="checkbox"
            checked={isBaseline}
            disabled={baselineExists}
            onChange={(e) => setIsBaseline(e.target.checked)}
          />
          Mark as baseline
          {baselineExists && (
            <span className="text-(--color-neutral-500)">(a baseline already exists)</span>
          )}
        </label>
      </div>

      {isFingerprint && versionQuery.isPending && (
        <p className="text-sm text-(--color-neutral-600)">Loading source parameters…</p>
      )}
      {isFingerprint && versionQuery.isError && (
        <ErrorState error={versionQuery.error} title="Failed to load source methodology" />
      )}

      {baseParams && stringsFor === baseParams && (
        <MethodologyParamForm
          values={strings}
          onFieldChange={(path, v) => setStrings((prev) => ({ ...prev, [path]: v }))}
        />
      )}

      {clientErrors.length > 0 && (
        <ul className="list-disc pl-5 text-sm text-(--color-neutral-700)" role="alert">
          {clientErrors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      {create.isError && (
        <p className="text-sm text-(--color-neutral-700)" role="alert" data-testid="create-error">
          {formatApiError(create.error)}
        </p>
      )}
      {create.isSuccess && (
        <p className="text-sm text-(--color-neutral-700)" data-testid="create-success">
          Stored methodology {create.data.fingerprint.slice(0, 12)}…
          {create.data.is_baseline ? " (baseline)" : ""}
        </p>
      )}

      <div className="flex gap-2">
        <button
          type="submit"
          className="btn btn-primary"
          disabled={create.isPending || !baseParams}
        >
          {create.isPending ? "Storing…" : "Store methodology version"}
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
