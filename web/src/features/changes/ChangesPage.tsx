import { Link } from "@tanstack/react-router";
import { type FormEvent, useMemo, useState } from "react";
import { buildCsvUrl, type Schemas } from "../../api/query";
import { FilterBar } from "../../components/controls";
import type { DataTableColumnDef } from "../../components/DataTable";
import { DataTable } from "../../components/DataTable";
import { EmptyState } from "../../components/states";
import { CorrectionBadge, RatingBadge } from "../securities/badges";
import { fmtTs, humanize } from "../securities/format";
import { useChanges } from "../shared/queries";
import { useRunSelection } from "../shared/run-state";

const PAGE_SIZE = 50;

type ChangeRow = Schemas["ChangeItem"];

const RATING_ACTIONS = [
  "initiation",
  "upgrade",
  "downgrade",
  "reiteration",
  "pt_change",
  "suspend",
  "restrict",
  "resume",
  "drop",
] as const;

const RUN_TYPES = ["run_created", "run_succeeded", "run_failed", "run_interrupted"] as const;

const TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "All types" },
  { value: "correction", label: "Corrections only" },
  ...RATING_ACTIONS.map((a) => ({ value: a, label: `Rating: ${humanize(a)}` })),
  ...RUN_TYPES.map((t) => ({ value: t, label: humanize(t) })),
];

interface FilterState {
  type: string;
  from: string;
  to: string;
  scopeRun: boolean;
}

/** `/changes` — filterable audit log over rating events and run lifecycle. */
export function ChangesPage() {
  const { runId } = useRunSelection();
  const [draft, setDraft] = useState<FilterState>({
    type: "",
    from: "",
    to: "",
    scopeRun: Boolean(runId),
  });
  const [applied, setApplied] = useState<FilterState>(draft);
  const [pageIndex, setPageIndex] = useState(0);

  const params = {
    run_id: applied.scopeRun ? runId : undefined,
    from: applied.from || undefined,
    to: applied.to || undefined,
    type: applied.type || undefined,
    limit: PAGE_SIZE,
    offset: pageIndex * PAGE_SIZE,
  };
  const changesQuery = useChanges(params);
  const rows = changesQuery.data?.data ?? [];
  const total = changesQuery.data?.pagination?.total ?? 0;

  const csvHref = buildCsvUrl("/api/changes", {
    run_id: params.run_id,
    from: params.from,
    to: params.to,
    type: params.type,
    limit: params.limit,
    offset: params.offset,
  });

  const apply = (event: FormEvent) => {
    event.preventDefault();
    setApplied({ ...draft });
    setPageIndex(0);
  };

  const columns = useMemo<DataTableColumnDef<ChangeRow>[]>(
    () => [
      {
        id: "ts",
        header: "Timestamp",
        accessorKey: "ts",
        cell: (ctx) => fmtTs(ctx.getValue<string>()),
      },
      {
        id: "kind",
        header: "Kind",
        accessorFn: (row) => row.kind,
        cell: (ctx) => {
          const row = ctx.row.original;
          const cls = row.kind === "run" ? "tag tag-accent-2" : "tag tag-neutral";
          return (
            <span className={cls} data-testid="change-kind">
              {row.kind === "run" ? "run" : "rating"}
            </span>
          );
        },
      },
      {
        id: "type",
        header: "Type",
        accessorKey: "type",
        cell: (ctx) => {
          const row = ctx.row.original;
          return (
            <span className="inline-flex items-center gap-1">
              <span className="tag tag-outline">{humanize(row.type)}</span>
              {row.is_correction && <CorrectionBadge />}
            </span>
          );
        },
      },
      {
        id: "actor",
        header: "Analyst",
        accessorFn: (row) => row.analyst_name ?? row.analyst_id ?? "",
        cell: (ctx) => {
          const row = ctx.row.original;
          if (!row.analyst_id) return "—";
          return (
            <Link
              to="/analysts/$analystId"
              params={{ analystId: row.analyst_id }}
              search={(prev) => ({ run_id: prev.run_id })}
              className="text-(--color-accent) hover:underline"
            >
              {row.analyst_name ?? row.analyst_id}
            </Link>
          );
        },
      },
      {
        id: "security",
        header: "Security",
        accessorFn: (row) => row.security_name ?? row.security_id ?? "",
        cell: (ctx) => {
          const row = ctx.row.original;
          if (!row.security_id) return row.security_name ?? "—";
          return (
            <Link
              to="/securities/$securityId"
              params={{ securityId: row.security_id }}
              search={(prev) => ({ run_id: prev.run_id })}
              className="text-(--color-accent) hover:underline"
            >
              {row.security_name ?? row.security_id}
            </Link>
          );
        },
      },
      {
        id: "transition",
        header: "Rating",
        accessorFn: (row) => row.new_rating ?? "",
        cell: (ctx) => {
          const row = ctx.row.original;
          if (row.kind === "run") {
            return (
              <span className="text-xs text-(--color-neutral-700)">
                {row.status ? `status: ${row.status}` : ""}
                {row.detail ? ` — ${row.detail}` : ""}
              </span>
            );
          }
          return (
            <span className="inline-flex items-center gap-1">
              <RatingBadge rating={row.old_rating} />
              <span aria-hidden="true">→</span>
              <RatingBadge rating={row.new_rating} />
            </span>
          );
        },
      },
      {
        id: "run",
        header: "Run",
        accessorKey: "run_id",
        cell: (ctx) => {
          const v = ctx.getValue<string | null | undefined>();
          return v ? <span className="font-mono text-xs">{v.slice(0, 8)}</span> : "—";
        },
      },
    ],
    [],
  );

  const scopeDisabled = !runId;

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="font-heading text-2xl font-semibold">Change log</h1>
        <p className="text-sm text-(--color-neutral-600)">
          Audit trail of rating actions and run lifecycle events.
        </p>
      </div>

      <FilterBar label="Change log filters">
        <form onSubmit={apply} className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            Type
            <select
              className="input"
              value={draft.type}
              onChange={(e) => setDraft({ ...draft, type: e.target.value })}
            >
              {TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            From
            <input
              type="date"
              className="input"
              value={draft.from}
              onChange={(e) => setDraft({ ...draft, from: e.target.value })}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            To
            <input
              type="date"
              className="input"
              value={draft.to}
              onChange={(e) => setDraft({ ...draft, to: e.target.value })}
            />
          </label>
          <label className="flex items-center gap-2 pb-1 text-xs text-(--color-neutral-700)">
            <input
              type="checkbox"
              checked={draft.scopeRun}
              disabled={scopeDisabled}
              onChange={(e) => setDraft({ ...draft, scopeRun: e.target.checked })}
            />
            Scope to active run{runId ? ` (${runId.slice(0, 8)})` : ""}
          </label>
          <button type="submit" className="btn btn-primary">
            Apply
          </button>
        </form>
      </FilterBar>

      {scopeDisabled && (
        <EmptyState
          title="No active run"
          description="The run scope filter is unavailable until a calc run is selected."
        />
      )}

      <DataTable<ChangeRow>
        columns={columns}
        data={rows}
        loading={changesQuery.isPending}
        error={changesQuery.isError ? changesQuery.error : undefined}
        onRetry={() => void changesQuery.refetch()}
        caption="Change log"
        emptyTitle="No changes"
        emptyDescription="No audit entries match the current filters."
        manualPagination
        pagination={{ pageIndex, pageSize: PAGE_SIZE }}
        onPaginationChange={(p) => setPageIndex(p.pageIndex)}
        rowCount={total}
        pageCount={Math.max(1, Math.ceil(total / PAGE_SIZE))}
        csvHref={csvHref}
        csvFilename="changes.csv"
      />
    </div>
  );
}
