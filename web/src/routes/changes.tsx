import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/changes")({
  component: () => <h1 className="font-heading text-2xl font-semibold">Change log</h1>,
});
