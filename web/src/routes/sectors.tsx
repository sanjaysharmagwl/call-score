import { createFileRoute } from "@tanstack/react-router";
import { SectorsPage } from "../features/sectors/SectorsPage";

export const Route = createFileRoute("/sectors")({
  component: SectorsPage,
});
