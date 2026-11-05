import { createFileRoute, Outlet, useRouterState } from "@tanstack/react-router";
import { BacktestsPage } from "../features/backtests/BacktestsPage";

/**
 * `/backtests` layout route: renders the list + benchmark sandbox at the
 * index and the `$backtestId` detail via <Outlet/> for child matches.
 * `run_id` is preserved in the URL for nav consistency (backtests are not
 * run-scoped and never receive it).
 */
function BacktestsLayout() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  if (pathname !== "/backtests") return <Outlet />;
  return <BacktestsPage />;
}

interface BacktestsSearch {
  tab?: "backtests" | "benchmarks";
  run_id?: string;
}

export const Route = createFileRoute("/backtests")({
  validateSearch: (search: Record<string, unknown>): BacktestsSearch => ({
    tab:
      typeof search.tab === "string" && (search.tab === "backtests" || search.tab === "benchmarks")
        ? search.tab
        : undefined,
    run_id: typeof search.run_id === "string" ? search.run_id : undefined,
  }),
  component: BacktestsLayout,
});
