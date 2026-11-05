import { createFileRoute, Outlet, useRouterState } from "@tanstack/react-router";
import { ScenariosPage } from "../features/scenarios/ScenariosPage";

/**
 * `/scenarios` layout route: renders the scenario list at the index and the
 * `$scenarioId` detail via <Outlet/> for child matches.
 */
function ScenariosLayout() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  if (pathname !== "/scenarios") return <Outlet />;
  return <ScenariosPage />;
}

export const Route = createFileRoute("/scenarios")({
  component: ScenariosLayout,
});
