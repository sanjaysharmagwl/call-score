import { useNavigate, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { shortFingerprint } from "../../api/query";
import { DataTable, type DataTableColumnDef } from "../../components/DataTable";
import { EmptyState, ErrorState, LoadingState } from "../../components/states";
import { useMethodologies } from "../shared/queries";
import {
  type MethodologyVersionSummary,
  useMethodologyDefaults,
  useMethodologyDiff,
  useMethodologyVersion,
} from "./api";
import { CreateMethodologyForm } from "./CreateMethodologyForm";
import { MethodologyDiffView } from "./MethodologyDiffView";
import { MethodologyParamsView } from "./MethodologyParamsView";

interface LabSearch {
  fp?: string;
  a?: string;
  b?: string;
}

function BaselineTag({ isBaseline }: { isBaseline: boolean }) {
  return isBaseline ? (
    <span className="tag tag-accent" data-testid="baseline-tag">
      baseline
    </span>
  ) : (
    <span className="tag tag-neutral">version</span>
  );
}

function VersionDetail({
  fingerprint,
  onDiffA,
  onDiffB,
  onClone,
}: {
  fingerprint: string;
  onDiffA: (fp: string) => void;
  onDiffB: (fp: string) => void;
  onClone: (fp: string) => void;
}) {
  const query = useMethodologyVersion(fingerprint);
  const version = query.data;

  return (
    <section className="card elev-sm" data-testid="methodology-detail">
      <div className="flex items-center justify-between gap-2">
        <div>
          <h3 className="card-title font-mono text-sm">{fingerprint}</h3>
          <div className="card-meta">
            {version && (
              <>
                <span>created {version.created_at.slice(0, 19).replace("T", " ")}</span>
                <BaselineTag isBaseline={version.is_baseline} />
              </>
            )}
          </div>
        </div>
        <div className="flex gap-2">
          <button type="button" className="btn btn-secondary" onClick={() => onDiffA(fingerprint)}>
            Diff A
          </button>
          <button type="button" className="btn btn-secondary" onClick={() => onDiffB(fingerprint)}>
            Diff B
          </button>
          <button type="button" className="btn btn-secondary" onClick={() => onClone(fingerprint)}>
            Clone as new
          </button>
        </div>
      </div>
      {query.isPending && <LoadingState label="Loading methodology" />}
      {query.isError && <ErrorState error={query.error} onRetry={() => void query.refetch()} />}
      {version && <MethodologyParamsView params={version.params ?? null} />}
    </section>
  );
}

function DiffPanel({
  a,
  b,
  versions,
  onChange,
}: {
  a: string;
  b: string;
  versions: MethodologyVersionSummary[];
  onChange: (a: string, b: string) => void;
}) {
  const diff = useMethodologyDiff(a, b);
  const options = versions.map((v) => v.fingerprint);
  return (
    <section className="card elev-sm" data-testid="methodology-diff-panel">
      <h3 className="card-title">Diff</h3>
      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          A
          <select
            className="input"
            value={a}
            onChange={(e) => onChange(e.target.value, b)}
            aria-label="Diff version A"
          >
            {options.map((fp) => (
              <option key={fp} value={fp}>
                {shortFingerprint(fp)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          B
          <select
            className="input"
            value={b}
            onChange={(e) => onChange(a, e.target.value)}
            aria-label="Diff version B"
          >
            {options.map((fp) => (
              <option key={fp} value={fp}>
                {shortFingerprint(fp)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <MethodologyDiffView
        diff={diff.data}
        isPending={diff.isPending}
        error={diff.error}
        onRetry={() => void diff.refetch()}
      />
    </section>
  );
}

/**
 * Methodology lab (`/methodologies`): browse immutable methodology versions,
 * inspect the full parameter tree, diff two fingerprints, and store new
 * versions built from the schema-driven parameter form.
 */
export function MethodologiesPage() {
  const search = useSearch({ strict: false }) as LabSearch;
  const navigate = useNavigate();
  const [showCreate, setShowCreate] = useState(false);
  const [cloneSource, setCloneSource] = useState<string | undefined>(undefined);

  const list = useMethodologies();
  const defaults = useMethodologyDefaults();
  const versions = list.data?.data ?? [];

  const fp = typeof search.fp === "string" ? search.fp : undefined;
  const diffA = typeof search.a === "string" ? search.a : undefined;
  const diffB = typeof search.b === "string" ? search.b : undefined;

  const setSearch = (patch: Partial<LabSearch>) => {
    void navigate({
      to: "/methodologies",
      search: (prev: Record<string, unknown>) => ({
        ...prev,
        run_id: typeof prev.run_id === "string" ? prev.run_id : undefined,
        ...patch,
      }),
    });
  };

  const columns: DataTableColumnDef<MethodologyVersionSummary>[] = [
    {
      accessorKey: "fingerprint",
      header: "Fingerprint",
      cell: ({ row }) => (
        <button
          type="button"
          className="font-mono text-xs text-(--color-accent) hover:underline"
          onClick={() => setSearch({ fp: row.original.fingerprint })}
        >
          {shortFingerprint(row.original.fingerprint)}
        </button>
      ),
    },
    {
      accessorKey: "created_at",
      header: "Created",
      cell: ({ row }) => row.original.created_at.slice(0, 19).replace("T", " "),
    },
    {
      accessorKey: "is_baseline",
      header: "Role",
      cell: ({ row }) => <BaselineTag isBaseline={row.original.is_baseline} />,
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-heading text-2xl font-semibold">Methodology lab</h1>
          <p className="text-sm text-(--color-neutral-600)">
            Immutable methodology versions. The baseline is the reference preset; new versions never
            replace it.
          </p>
        </div>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            setCloneSource(undefined);
            setShowCreate((v) => !v);
          }}
        >
          {showCreate ? "Hide new version" : "New version"}
        </button>
      </div>

      {showCreate && (
        <CreateMethodologyForm
          defaults={defaults.data}
          versions={versions}
          initialSource={cloneSource}
          onCancel={() => setShowCreate(false)}
          onCreated={(v) => {
            // Keep the form open so the success confirmation stays visible;
            // the new version is also selected in the detail panel below.
            setSearch({ fp: v.fingerprint });
          }}
        />
      )}

      <DataTable
        columns={columns}
        data={versions}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        emptyTitle="No methodology versions"
        emptyDescription="Store the first version with “New version” (built-in defaults are preloaded)."
        caption="Stored methodology versions"
      />

      {fp && (
        <VersionDetail
          fingerprint={fp}
          onDiffA={(f) =>
            setSearch({
              a: f,
              b: diffB ?? versions.find((v) => v.fingerprint !== f)?.fingerprint ?? f,
            })
          }
          onDiffB={(f) => setSearch({ b: f })}
          onClone={(f) => {
            setCloneSource(f);
            setShowCreate(true);
          }}
        />
      )}

      {diffA && diffB && diffA !== diffB && (
        <DiffPanel
          a={diffA}
          b={diffB}
          versions={versions}
          onChange={(a, b) => setSearch({ a, b })}
        />
      )}
      {diffA && diffB && diffA === diffB && (
        <EmptyState title="Pick two different versions" description="A and B must differ." />
      )}
    </div>
  );
}
