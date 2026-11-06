import { Link } from "@tanstack/react-router";
import { useMemo } from "react";
import type { Schemas } from "../../api/query";
import { ProvenanceLabel, ScoreBadge } from "../../components/Badges";
import { BarChart } from "../../components/charts";
import { MetricCard } from "../../components/MetricCard";
import { EmptyState, ErrorState, LoadingState } from "../../components/states";
import { DelistedBadge } from "../securities/badges";
import { fmtNum } from "../securities/format";
import { useSectorPerformance, useSectors } from "../shared/queries";
import { useRunSelection } from "../shared/run-state";

type ScoreBucket = Schemas["ScoreBucket"];

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex flex-col">
      <dt className="text-xs text-(--color-neutral-500)">{label}</dt>
      <dd className="text-sm font-medium">{value}</dd>
    </div>
  );
}

/** `/sectors/$sectorId` — sector detail + run-scoped performance. */
export function SectorDetailPage({ sectorId }: { sectorId: string }) {
  const sectorsQuery = useSectors({ limit: 500 });
  const { runId } = useRunSelection();
  const perfQuery = useSectorPerformance(sectorId, runId);

  const sector = useMemo(
    () => sectorsQuery.data?.data?.find((s) => s.sector_id === sectorId) ?? null,
    [sectorsQuery.data, sectorId],
  );
  const perf = perfQuery.data;

  if (sectorsQuery.isPending) {
    return <LoadingState label="Loading sector" />;
  }
  if (sectorsQuery.isError) {
    return (
      <ErrorState
        error={sectorsQuery.error}
        onRetry={() => void sectorsQuery.refetch()}
        title="Failed to load sector"
      />
    );
  }
  if (!sector) {
    return (
      <EmptyState
        title="Unknown sector"
        description={`No sector with id "${sectorId}" exists in the security master.`}
        action={
          <Link
            to="/sectors"
            search={(prev) => ({ run_id: prev.run_id })}
            className="btn btn-secondary"
          >
            Back to sectors
          </Link>
        }
      />
    );
  }

  const distribution = (perf?.composite_score_distribution ?? []) as ScoreBucket[];
  const distData = distribution.map((b) => ({ x: b.bucket, y: b.count }));

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="font-heading text-2xl font-semibold">{sector.name}</h1>
          {sector.delisted_count > 0 && <DelistedBadge />}
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-(--color-neutral-700)">
          <span className="font-mono text-xs">{sector.sector_id}</span>
          {sector.parent_sector_id && (
            <span className="text-(--color-neutral-600)">group: {sector.parent_sector_id}</span>
          )}
          <Link
            to="/securities"
            search={(prev) => ({
              q: prev.q,
              sector: sector.sector_id,
              cap_bucket: prev.cap_bucket,
              as_of: prev.as_of,
              run_id: prev.run_id,
            })}
            className="text-(--color-accent) hover:underline"
          >
            View securities in this sector →
          </Link>
        </div>
      </div>

      <section className="card elev-sm">
        <h2 className="card-title">Coverage</h2>
        <dl className="grid grid-cols-2 gap-4 md:grid-cols-3">
          <Stat label="Securities" value={sector.security_count} />
          <Stat label="Covering analysts" value={sector.analyst_count} />
          <Stat label="Delisted securities" value={sector.delisted_count} />
        </dl>
      </section>

      {!runId ? (
        <EmptyState
          title="No active run"
          description="Select a calc run to see this sector's run-scoped performance."
        />
      ) : perfQuery.isPending ? (
        <LoadingState label="Loading sector performance" />
      ) : perfQuery.isError ? (
        <ErrorState
          error={perfQuery.error}
          onRetry={() => void perfQuery.refetch()}
          title="Failed to load sector performance"
        />
      ) : perf ? (
        <section className="card elev-sm flex flex-col gap-3">
          <div className="flex items-center justify-between gap-2">
            <h2 className="card-title">Run performance</h2>
            <ProvenanceLabel
              run_id={perf.run.run_id}
              run_status={perf.run.status}
              methodology_fingerprint={perf.run.methodology_fingerprint}
              as_of_date={perf.run.as_of_date}
              knowledge_cutoff={perf.run.knowledge_cutoff}
              algorithm_version={perf.run.algorithm_version}
            />
          </div>
          <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
            <MetricCard title="Mean composite">
              <ScoreBadge score={perf.mean_composite_score} digits={2} />
            </MetricCard>
            <MetricCard title="Ranked analysts">
              <span className="font-heading text-2xl font-semibold">
                {perf.n_ranked}
                <span className="text-sm font-normal text-(--color-neutral-600)">
                  {" "}
                  / {perf.n_analysts}
                </span>
              </span>
            </MetricCard>
            <MetricCard title="Securities">
              <span className="font-heading text-2xl font-semibold">{perf.n_securities}</span>
              <span className="text-xs text-(--color-neutral-600)">
                {perf.n_with_prices} with prices · {perf.n_delisted} delisted
              </span>
            </MetricCard>
          </div>
          {distData.length > 0 ? (
            <BarChart
              title="Composite score distribution"
              data={distData}
              height={200}
              formatY={(v) => fmtNum(v, 0)}
            />
          ) : (
            <EmptyState
              title="No composite scores"
              description="No ranked analysts cover this sector in the selected run."
            />
          )}
        </section>
      ) : null}
    </div>
  );
}
