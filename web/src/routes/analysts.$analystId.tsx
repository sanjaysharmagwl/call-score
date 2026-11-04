import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/analysts/$analystId")({
  component: () => <h1 className="font-heading text-2xl font-semibold">Analyst</h1>,
});
