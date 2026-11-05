import { createFileRoute } from "@tanstack/react-router";
import { ChangesPage } from "../features/changes/ChangesPage";

export const Route = createFileRoute("/changes")({
  component: ChangesPage,
});
