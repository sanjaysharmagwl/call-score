import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { shortFingerprint, shortRunId } from "../../api/query";
import { DataTable, type DataTableColumnDef } from "../../components/DataTable";
import { summarizeOverrides } from "../methodologies/fields";
import { useRuns } from "../shared/queries";
import { type ScenarioSummary, useScenarios } from "./api";
import { ScenarioForm } from "./ScenarioForm";

function WhatIfTag() {
  return (
    <span
      className="tag tag-accent-2"
      data-testid="whatif-tag"
      title="What-if scenario — never mutates the baseline"
    >
      what-if
    </span>
  );
}

/**
 * Scenario list (`/scenarios`): saved what-if scenarios with base
 * methodology, overrides summary and run count.
 */
export function ScenariosPage() {
  const navigate = useNavigate();
  const [showCreate, setShowCreate] = useState(false);
  const scenarios = useScenarios({ limit: 200 });
  const runs = useRuns({ limit: 500, order: "desc" });
  const runStatus = new Map((runs.data?.data ?? []).map((r) => [r.run_id, r.status]));

  const columns: DataTableColumnDef<ScenarioSummary>[] = [
    {
      accessorKey: "name",
      header: "Name",
      cell: ({ row }) => (
        <span className="flex items-center gap-2">
          <Link
            to="/scenarios/$scenarioId"
            params={{ scenarioId: row.original.scenario_id }}
            search={(prev) => ({ run_id: prev.run_id })}
            className="text-(--color-accent) hover:underline"
          >
            {row.original.name}
          </Link>
          <WhatIfTag />
        </span>
      ),
    },
    {
      id: "base",
      header: "Base",
      cell: ({ row }) =>
        row.original.base_ref ??
        (row.original.base_fingerprint
          ? shortFingerprint(row.original.base_fingerprint)
          : "default"),
    },
    {
      id: "overrides",
      header: "Overrides",
      cell: ({ row }) => (
        <span className="font-mono text-xs">{summarizeOverrides(row.original.overrides)}</span>
      ),
    },
    {
      accessorKey: "run_count",
      header: "Runs",
      cell: ({ row }) => row.original.run_count,
    },
    {
      id: "last_run",
      header: "Last run",
      cell: ({ row }) => {
        const id = row.original.last_run_id;
        if (!id) return "—";
        const status = runStatus.get(id);
        return (
          <span className="font-mono text-xs" title={id}>
            {shortRunId(id)}
            {status ? ` · ${status}` : ""}
          </span>
        );
      },
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-heading text-2xl font-semibold">Scenarios</h1>
          <p className="text-sm text-(--color-neutral-600)">
            What-if experiments: a base methodology plus overrides, executed as separate runs.
            Scenario runs never modify the baseline.
          </p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setShowCreate((v) => !v)}>
          {showCreate ? "Hide new scenario" : "New scenario"}
        </button>
      </div>

      {showCreate && (
        <ScenarioForm
          onCancel={() => setShowCreate(false)}
          onCreated={(scenario) => {
            setShowCreate(false);
            void navigate({
              to: "/scenarios/$scenarioId",
              params: { scenarioId: scenario.scenario_id },
              search: (prev) => ({ run_id: prev.run_id }),
            });
          }}
        />
      )}

      <DataTable
        columns={columns}
        data={scenarios.data?.data ?? []}
        loading={scenarios.isPending}
        error={scenarios.error}
        onRetry={() => void scenarios.refetch()}
        emptyTitle="No scenarios yet"
        emptyDescription="Create a what-if scenario to compare methodology overrides against the baseline."
        caption="Saved scenarios"
      />
    </div>
  );
}
