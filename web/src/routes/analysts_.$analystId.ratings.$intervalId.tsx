import { createFileRoute } from "@tanstack/react-router";
import { RatingDetailPage } from "../features/ratings/RatingDetailPage";

export const Route = createFileRoute("/analysts_/$analystId/ratings/$intervalId")({
  component: RatingDetailRoute,
});

function RatingDetailRoute() {
  const { analystId, intervalId } = Route.useParams();
  return <RatingDetailPage analystId={analystId} intervalId={intervalId} />;
}
