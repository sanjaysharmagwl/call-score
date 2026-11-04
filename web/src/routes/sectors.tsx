import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/sectors")({
  component: () => <h1 className="font-heading text-2xl font-semibold">Sectors</h1>,
});
