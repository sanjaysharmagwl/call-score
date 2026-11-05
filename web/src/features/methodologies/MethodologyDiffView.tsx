import { ErrorState, LoadingState } from "../../components/states";
import type { MethodologyDiffResponse } from "./api";
import { formatLeafValue } from "./fields";

function valueCell(value: unknown, missing: boolean) {
  if (missing) return <span className="text-(--color-neutral-400)">— absent —</span>;
  return <code className="text-xs">{formatLeafValue(value)}</code>;
}

/** Field-level diff table for `GET /api/methodologies/diff`. */
export function MethodologyDiffView({
  diff,
  isPending,
  error,
  onRetry,
}: {
  diff: MethodologyDiffResponse | undefined;
  isPending: boolean;
  error: unknown;
  onRetry?: () => void;
}) {
  if (isPending) return <LoadingState label="Loading methodology diff" />;
  if (error) return <ErrorState error={error} onRetry={onRetry} title="Failed to load diff" />;
  if (!diff) return null;

  if (diff.identical || !diff.changes || diff.changes.length === 0) {
    return (
      <p className="text-sm text-(--color-neutral-600)" data-testid="methodology-diff-identical">
        The two methodology versions are identical.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto" data-testid="methodology-diff">
      <table className="table">
        <caption className="sr-only">Methodology field differences</caption>
        <thead>
          <tr>
            <th scope="col">Field</th>
            <th scope="col">A</th>
            <th scope="col">B</th>
          </tr>
        </thead>
        <tbody>
          {diff.changes.map((change) => (
            <tr key={change.path}>
              <td className="font-mono text-xs">{change.path}</td>
              <td>{valueCell(change.a, change.a_missing)}</td>
              <td>{valueCell(change.b, change.b_missing)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-1 text-xs text-(--color-neutral-500)">
        A = {diff.a_fingerprint.slice(0, 12)}… · B = {diff.b_fingerprint.slice(0, 12)}… (
        {diff.changes.length} field{diff.changes.length === 1 ? "" : "s"} differ)
      </p>
    </div>
  );
}
