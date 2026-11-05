import { createFileRoute } from "@tanstack/react-router";
import { AnalystDetailPage } from "../features/analysts/AnalystDetailPage";

export const Route = createFileRoute("/analysts/$analystId")({
  component: AnalystDetailRoute,
});

function AnalystDetailRoute() {
  const { analystId } = Route.useParams();
  return <AnalystDetailPage analystId={analystId} />;
}
