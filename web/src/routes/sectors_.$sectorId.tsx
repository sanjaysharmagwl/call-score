import { createFileRoute } from "@tanstack/react-router";
import { SectorDetailPage } from "../features/sectors/SectorDetailPage";

function SectorDetailRoute() {
  const { sectorId } = Route.useParams();
  return <SectorDetailPage sectorId={sectorId} />;
}

export const Route = createFileRoute("/sectors_/$sectorId")({
  component: SectorDetailRoute,
});
