import type { ReactNode } from "react";
import { shortFingerprint, shortRunId } from "../api/query";

/** Composite score with optional rank context. */
export function ScoreBadge({
  score,
  rank,
  digits = 1,
}: {
  score?: number | null;
  rank?: number | null;
  digits?: number;
}) {
  return (
    <span className="inline-flex items-baseline gap-2" data-testid="score-badge">
      <span className="font-heading text-lg font-semibold">
        {score === null || score === undefined ? "—" : score.toFixed(digits)}
      </span>
      {rank !== null && rank !== undefined && (
        <span className="text-xs text-(--color-neutral-600)">#{rank}</span>
      )}
    </span>
  );
}

/** Ranked / unranked eligibility with the unranked reason when present. */
export function EligibilityBadge({
  isRanked,
  unrankedReason,
}: {
  isRanked: boolean;
  unrankedReason?: string | null;
}) {
  if (isRanked) {
    return (
      <span className="tag tag-accent" data-testid="eligibility-badge">
        Ranked
      </span>
    );
  }
  return (
    <span
      className="tag tag-neutral"
      data-testid="eligibility-badge"
      title={unrankedReason ?? undefined}
    >
      Unranked{unrankedReason ? ` — ${unrankedReason}` : ""}
    </span>
  );
}

export interface ProvenanceFields {
  run_id?: string | null;
  run_status?: string | null;
  methodology_fingerprint?: string | null;
  as_of_date?: string | null;
  knowledge_cutoff?: string | null;
  algorithm_version?: string | null;
}

/**
 * Compact provenance line: which run/methodology/as-of produced a payload.
 * Full ids are available on hover via `title`.
 */
export function ProvenanceLabel({
  run_id,
  run_status,
  methodology_fingerprint,
  as_of_date,
  knowledge_cutoff,
  algorithm_version,
  className = "",
}: ProvenanceFields & { className?: string }) {
  const parts: { key: string; node: ReactNode }[] = [];

  if (run_id) {
    parts.push({
      key: "run",
      node: (
        <span title={`run_id: ${run_id}`} data-testid="provenance-run">
          run {shortRunId(run_id)}
          {run_status ? ` (${run_status})` : ""}
        </span>
      ),
    });
  }
  if (methodology_fingerprint) {
    parts.push({
      key: "meth",
      node: (
        <span
          title={`methodology_fingerprint: ${methodology_fingerprint}`}
          data-testid="provenance-methodology"
        >
          meth {shortFingerprint(methodology_fingerprint)}
        </span>
      ),
    });
  }
  if (as_of_date) {
    parts.push({
      key: "asof",
      node: (
        <span title={`as_of_date: ${as_of_date}`} data-testid="provenance-as-of">
          as of {as_of_date}
        </span>
      ),
    });
  }
  if (knowledge_cutoff) {
    parts.push({
      key: "cutoff",
      node: (
        <span title={`knowledge_cutoff: ${knowledge_cutoff}`} data-testid="provenance-cutoff">
          cutoff {knowledge_cutoff.slice(0, 10)}
        </span>
      ),
    });
  }
  if (algorithm_version) {
    parts.push({
      key: "algo",
      node: <span title={`algorithm_version: ${algorithm_version}`}>{algorithm_version}</span>,
    });
  }

  if (parts.length === 0) return null;

  return (
    <span
      className={`inline-flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-(--color-neutral-600) ${className}`}
      data-testid="provenance-label"
    >
      {parts.map((part, i) => (
        <span key={part.key} className="inline-flex items-center gap-3">
          {i > 0 && <span aria-hidden="true">·</span>}
          {part.node}
        </span>
      ))}
    </span>
  );
}
