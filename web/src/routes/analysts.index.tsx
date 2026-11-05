import { createFileRoute } from "@tanstack/react-router";
import { AnalystsPage } from "../features/analysts/AnalystsPage";

export const Route = createFileRoute("/analysts/")({
  // Keep the deep-linkable filters in the URL alongside the root run_id.
  validateSearch: (search: Record<string, unknown>) => ({
    q: typeof search.q === "string" ? search.q : undefined,
    peer_group: typeof search.peer_group === "string" ? search.peer_group : undefined,
    eligible: typeof search.eligible === "string" ? search.eligible : undefined,
    run_id: typeof search.run_id === "string" ? search.run_id : undefined,
  }),
  component: AnalystsPage,
});
