import { createFileRoute } from "@tanstack/react-router";
import { LeaderboardPage } from "../features/leaderboard/LeaderboardPage";

export const Route = createFileRoute("/leaderboard")({
  // Keep the deep-linkable filters in the URL alongside the root run_id.
  validateSearch: (search: Record<string, unknown>) => ({
    peer_group: typeof search.peer_group === "string" ? search.peer_group : undefined,
    metric: typeof search.metric === "string" ? search.metric : undefined,
    min_obs: typeof search.min_obs === "string" ? search.min_obs : undefined,
    eligible: typeof search.eligible === "string" ? search.eligible : undefined,
    run_id: typeof search.run_id === "string" ? search.run_id : undefined,
  }),
  component: LeaderboardPage,
});
