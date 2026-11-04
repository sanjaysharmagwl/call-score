import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/leaderboard")({
  component: () => <h1 className="font-heading text-2xl font-semibold">Leaderboard</h1>,
});
