import { createFileRoute } from "@tanstack/react-router";
import { SecurityDetailPage } from "../features/securities/SecurityDetailPage";

function SecurityDetailRoute() {
  const { securityId } = Route.useParams();
  return <SecurityDetailPage securityId={securityId} />;
}

export const Route = createFileRoute("/securities_/$securityId")({
  component: SecurityDetailRoute,
});
