import { createFileRoute } from "@tanstack/react-router";
import { ScenarioDetailPage, type ScenarioTab } from "../../features/scenarios/ScenarioDetailPage";

interface ScenarioSearch {
  tab?: ScenarioTab;
}

const VALID_TABS = new Set<ScenarioTab>(["config", "compare", "sweep"]);

function ScenarioDetailRoute() {
  const { scenarioId } = Route.useParams();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const tab = search.tab && VALID_TABS.has(search.tab) ? search.tab : "config";
  return (
    <ScenarioDetailPage
      scenarioId={scenarioId}
      tab={tab}
      onTabChange={(t) =>
        void navigate({
          to: ".",
          search: (prev: Record<string, unknown>) => ({
            ...prev,
            run_id: typeof prev.run_id === "string" ? prev.run_id : undefined,
            tab: t,
          }),
          replace: true,
        })
      }
    />
  );
}

export const Route = createFileRoute("/scenarios/$scenarioId")({
  validateSearch: (search: Record<string, unknown>): ScenarioSearch => ({
    tab:
      typeof search.tab === "string" && VALID_TABS.has(search.tab as ScenarioTab)
        ? (search.tab as ScenarioTab)
        : undefined,
  }),
  component: ScenarioDetailRoute,
});
