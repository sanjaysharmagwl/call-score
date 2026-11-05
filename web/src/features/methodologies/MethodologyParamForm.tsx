import type { LeafField } from "./fields";
import { SEED_LEAF_FIELDS, sections } from "./fields";

export interface MethodologyParamFormProps {
  /** Per-leaf string values keyed by dot path (see `methodologyToStrings`). */
  values: Record<string, string>;
  onFieldChange: (path: string, value: string) => void;
  /** Optional baseline strings; differing leaves get a visual marker. */
  baseline?: Record<string, string>;
  disabled?: boolean;
}

function fieldId(path: string): string {
  return `mf-${path.replace(/[^a-zA-Z0-9]+/g, "-")}`;
}

function changed(
  values: Record<string, string>,
  baseline: Record<string, string> | undefined,
  path: string,
) {
  return baseline !== undefined && values[path] !== baseline[path];
}

function Control({
  field,
  value,
  onChange,
  disabled,
  marked,
}: {
  field: LeafField;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  marked?: boolean;
}) {
  const id = fieldId(field.path);
  const cls = `input${marked ? " border-(--color-accent)" : ""}`;

  if (field.readOnly) {
    return (
      <output className="input bg-(--color-neutral-100) text-(--color-neutral-600)">{value}</output>
    );
  }
  if (field.kind === "enum") {
    return (
      <select
        id={id}
        className={cls}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        {(field.options ?? []).map((opt) => (
          <option key={opt} value={opt}>
            {opt}
          </option>
        ))}
      </select>
    );
  }
  if (field.kind === "boolean") {
    return (
      <select
        id={id}
        className={cls}
        value={value === "true" ? "true" : "false"}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    );
  }
  const numeric = field.kind === "int" || field.kind === "number";
  return (
    <input
      id={id}
      type={numeric ? "number" : "text"}
      step={field.kind === "int" ? 1 : "any"}
      className={cls}
      value={value}
      disabled={disabled}
      placeholder={field.nullable ? "null" : undefined}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

/**
 * Schema-driven editor for the methodology parameter tree. Renders every
 * editable leaf field grouped into labelled sections; values are kept as
 * strings and coerced on submit (see `applyStringsToMethodology`).
 */
export function MethodologyParamForm({
  values,
  onFieldChange,
  baseline,
  disabled = false,
}: MethodologyParamFormProps) {
  const editable = new Set(SEED_LEAF_FIELDS.map((f) => f.path));
  return (
    <div className="flex flex-col gap-4" data-testid="methodology-param-form">
      {sections().map((section) => (
        <fieldset key={section.name} className="border border-(--color-divider) p-3">
          <legend className="px-1 text-xs font-semibold uppercase tracking-[0.08em] text-(--color-neutral-600)">
            {section.name}
          </legend>
          <div className="grid grid-cols-2 gap-3">
            {section.fields.map((field) => {
              const isEditable = editable.has(field.path);
              const mark = isEditable && changed(values, baseline, field.path);
              return (
                <div key={field.path} className="field">
                  <label htmlFor={fieldId(field.path)}>
                    {field.label}
                    {mark && (
                      <span className="ml-2 text-[10px] uppercase text-(--color-accent)">
                        changed
                      </span>
                    )}
                  </label>
                  <Control
                    field={field}
                    value={values[field.path] ?? ""}
                    onChange={(v) => onFieldChange(field.path, v)}
                    disabled={disabled || field.readOnly}
                    marked={mark}
                  />
                  {field.hint && (
                    <p className="mt-1 text-[11px] text-(--color-neutral-500)">{field.hint}</p>
                  )}
                </div>
              );
            })}
          </div>
        </fieldset>
      ))}
    </div>
  );
}
