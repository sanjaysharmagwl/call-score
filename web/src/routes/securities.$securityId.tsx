import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/securities/$securityId")({
  component: () => <h1 className="font-heading text-2xl font-semibold">Security</h1>,
});
