import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { Schemas } from "../../api/query";

export type RatingDetailResponse = Schemas["RatingDetailResponse"];

async function unwrap<T>(promise: Promise<{ data?: T; error?: unknown }>): Promise<T> {
  const { data, error } = await promise;
  if (error) throw error;
  if (data === undefined) throw new Error("Empty API response");
  return data;
}

/** Single rating interval with legs, returns, contributions and explanation. */
export function useRatingDetail(analystId: string, intervalId: string, runId: string | undefined) {
  return useQuery({
    queryKey: [
      "analyst-rating-detail",
      { analyst_id: analystId, interval_id: intervalId, run_id: runId ?? "" },
    ] as const,
    queryFn: () =>
      unwrap<RatingDetailResponse>(
        api.GET("/api/analysts/{analyst_id}/ratings/{interval_id}", {
          params: {
            path: { analyst_id: analystId, interval_id: intervalId },
            query: { run_id: runId ?? "" },
          },
        }),
      ),
    enabled: Boolean(analystId) && Boolean(intervalId) && Boolean(runId),
    retry: 1,
  });
}
