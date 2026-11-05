import { createFileRoute } from "@tanstack/react-router";
import { SecuritiesPage } from "../features/securities/SecuritiesPage";

export const Route = createFileRoute("/securities")({
  // Keep the deep-linkable list filters in the URL alongside the root run_id.
  validateSearch: (search: Record<string, unknown>) => ({
    q: typeof search.q === "string" ? search.q : undefined,
    sector: typeof search.sector === "string" ? search.sector : undefined,
    cap_bucket: typeof search.cap_bucket === "string" ? search.cap_bucket : undefined,
    as_of: typeof search.as_of === "string" ? search.as_of : undefined,
    run_id: typeof search.run_id === "string" ? search.run_id : undefined,
  }),
  component: SecuritiesPage,
});
