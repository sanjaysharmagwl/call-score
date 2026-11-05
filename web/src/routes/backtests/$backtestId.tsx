import { createFileRoute } from "@tanstack/react-router";
import { BacktestDetailPage } from "../../features/backtests/BacktestDetailPage";

function BacktestDetailRoute() {
  const { backtestId } = Route.useParams();
  return <BacktestDetailPage backtestId={backtestId} />;
}

export const Route = createFileRoute("/backtests/$backtestId")({
  component: BacktestDetailRoute,
});
