import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { Schemas } from "../../api/query";
import { queryKeys } from "../../api/query";

export type RatingListResponse = Schemas["RatingListResponse"];
export type RatingListItem = Schemas["RatingListItem"];
export type PortfolioResponse = Schemas["PortfolioResponse"];
export type NavPoint = Schemas["NavPoint"];

async function unwrap<T>(promise: Promise<{ data?: T; error?: unknown }>): Promise<T> {
  const { data, error } = await promise;
  if (error) throw error;
  if (data === undefined) throw new Error("Empty API response");
  return data;
}

export interface AnalystRatingsParams {
  run_id: string;
  from?: string;
  to?: string;
  action?: string;
  security?: string;
  sort?: string;
  order?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

/** Paged rating intervals for one analyst in one run. */
export function useAnalystRatings(analystId: string, params: AnalystRatingsParams, enabled = true) {
  return useQuery({
    queryKey: queryKeys.analysts.ratings(analystId, params as unknown as Record<string, unknown>),
    queryFn: () =>
      unwrap<RatingListResponse>(
        api.GET("/api/analysts/{analyst_id}/ratings", {
          params: {
            path: { analyst_id: analystId },
            query: {
              run_id: params.run_id,
              from: params.from,
              to: params.to,
              action: params.action,
              security: params.security,
              sort: params.sort,
              order: params.order,
              limit: params.limit,
              offset: params.offset,
            },
          },
        }),
      ),
    enabled: enabled && Boolean(analystId) && Boolean(params.run_id),
    retry: 1,
  });
}

/** NAV/drawdown series (server-downsampled) + latest positions snapshot. */
export function useAnalystPortfolio(analystId: string, runId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.analysts.portfolio(analystId, runId),
    queryFn: () =>
      unwrap<PortfolioResponse>(
        api.GET("/api/analysts/{analyst_id}/portfolio", {
          params: {
            path: { analyst_id: analystId },
            query: { run_id: runId ?? "" },
          },
        }),
      ),
    enabled: Boolean(analystId) && Boolean(runId),
    retry: 1,
  });
}
