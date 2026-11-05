import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { Schemas } from "../../api/query";
import { queryKeys } from "../../api/query";

export type PricesResponse = Schemas["PricesResponse"];
export type RatingsResponse = Schemas["RatingsResponse"];
export type RatingEventItem = Schemas["RatingEventItem"];
export type ConsensusResponse = Schemas["ConsensusResponse"];
export type ConsensusPoint = Schemas["ConsensusPoint"];
export type TargetOutcomeItem = Schemas["TargetOutcomeItem"];
export type SecurityPerformanceResponse = Schemas["SecurityPerformanceResponse"];
export type HorizonAggregate = Schemas["HorizonAggregate"];

async function unwrap<T>(promise: Promise<{ data?: T; error?: unknown }>): Promise<T> {
  const { data, error } = await promise;
  if (error) throw error;
  if (data === undefined) throw new Error("Empty API response");
  return data;
}

export interface SecurityPricesParams {
  interval?: string;
  from?: string;
  to?: string;
  adjusted?: boolean;
  limit?: number;
  offset?: number;
}

/** OHLC/adjustment/total-return series for one security. Not run-scoped. */
export function useSecurityPrices(
  securityId: string,
  params?: SecurityPricesParams,
  enabled = true,
) {
  return useQuery({
    queryKey: queryKeys.securities.prices(securityId, params as Record<string, unknown>),
    queryFn: () =>
      unwrap<PricesResponse>(
        api.GET("/api/securities/{security_id}/prices", {
          params: {
            path: { security_id: securityId },
            query: {
              interval: params?.interval,
              from: params?.from,
              to: params?.to,
              adjusted: params?.adjusted,
              limit: params?.limit,
              offset: params?.offset,
            },
          },
        }),
      ),
    enabled: enabled && Boolean(securityId),
    retry: 1,
  });
}

export interface SecurityRatingsParams {
  run_id?: string;
  from?: string;
  to?: string;
  limit?: number;
  offset?: number;
}

/** Rating events for one security; `run_id` adds interval outcomes. */
export function useSecurityRatings(
  securityId: string,
  params?: SecurityRatingsParams,
  enabled = true,
) {
  return useQuery({
    queryKey: queryKeys.securities.ratings(
      securityId,
      params as unknown as Record<string, unknown>,
    ),
    queryFn: () =>
      unwrap<RatingsResponse>(
        api.GET("/api/securities/{security_id}/ratings", {
          params: {
            path: { security_id: securityId },
            query: {
              run_id: params?.run_id,
              from: params?.from,
              to: params?.to,
              limit: params?.limit,
              offset: params?.offset,
            },
          },
        }),
      ),
    enabled: enabled && Boolean(securityId),
    retry: 1,
  });
}

export interface SecurityConsensusParams {
  run_id?: string;
  from?: string;
  to?: string;
}

/** Consensus daily series; `run_id` adds that run's target outcomes. */
export function useSecurityConsensus(
  securityId: string,
  params?: SecurityConsensusParams,
  enabled = true,
) {
  return useQuery({
    queryKey: queryKeys.securities.consensus(
      securityId,
      params as unknown as Record<string, unknown>,
    ),
    queryFn: () =>
      unwrap<ConsensusResponse>(
        api.GET("/api/securities/{security_id}/consensus", {
          params: {
            path: { security_id: securityId },
            query: {
              run_id: params?.run_id,
              from: params?.from,
              to: params?.to,
            },
          },
        }),
      ),
    enabled: enabled && Boolean(securityId),
    retry: 1,
  });
}

/** Aggregate interval stats (n ratings, mean excess, hit rate) for a run. */
export function useSecurityPerformance(securityId: string, runId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.securities.performance(securityId, runId ?? ""),
    queryFn: () =>
      unwrap<SecurityPerformanceResponse>(
        api.GET("/api/securities/{security_id}/performance", {
          params: { path: { security_id: securityId }, query: { run_id: runId ?? "" } },
        }),
      ),
    enabled: Boolean(securityId) && Boolean(runId),
    retry: 1,
  });
}
