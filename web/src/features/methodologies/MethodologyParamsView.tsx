import type { JsonObject } from "./fields";
import { formatLeafValue, getPathValue, LEAF_FIELDS, sections } from "./fields";

/** Read-only rendering of a full methodology parameter tree, sectioned. */
export function MethodologyParamsView({ params }: { params: JsonObject | null | undefined }) {
  if (!params) {
    return (
      <p className="text-sm text-(--color-neutral-600)">No parameters stored for this version.</p>
    );
  }
  return (
    <div className="flex flex-col gap-4" data-testid="methodology-params-view">
      {sections().map((section) => (
        <section key={section.name}>
          <h4 className="mb-1 text-xs font-semibold uppercase tracking-[0.08em] text-(--color-neutral-600)">
            {section.name}
          </h4>
          <dl className="grid grid-cols-[220px_1fr] gap-x-4 gap-y-1 text-sm">
            {section.fields.map((field) => (
              <div key={field.path} className="contents">
                <dt className="text-(--color-neutral-500)">{field.label}</dt>
                <dd className="font-mono text-xs" data-field={field.path}>
                  {formatLeafValue(getPathValue(params, field.path))}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
      {/* Render any params keys not covered by the field spec (forward-compat). */}
      <ExtraParams params={params} />
    </div>
  );
}

function ExtraParams({ params }: { params: JsonObject }) {
  const known = new Set(LEAF_FIELDS.map((f) => f.path.split(".")[0]));
  const extra = Object.keys(params).filter((k) => !known.has(k));
  if (extra.length === 0) return null;
  return (
    <section>
      <h4 className="mb-1 text-xs font-semibold uppercase tracking-[0.08em] text-(--color-neutral-600)">
        Other
      </h4>
      <dl className="grid grid-cols-[220px_1fr] gap-x-4 gap-y-1 text-sm">
        {extra.map((key) => (
          <div key={key} className="contents">
            <dt className="text-(--color-neutral-500)">{key}</dt>
            <dd className="font-mono text-xs">{formatLeafValue(params[key])}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
