import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { Schemas } from "../../api/query";
import { queryKeys } from "../../api/query";

export type MetricCatalogResponse = Schemas["MetricCatalogResponse"];

async function unwrap<T>(promise: Promise<{ data?: T; error?: unknown }>): Promise<T> {
  const { data, error } = await promise;
  if (error) throw error;
  if (data === undefined) throw new Error("Empty API response");
  return data;
}

/** Static metric catalog (codes + families) for filter dropdowns. Not run-scoped. */
export function useMetricCatalog() {
  return useQuery({
    queryKey: queryKeys.meta.metrics(),
    queryFn: () => unwrap<MetricCatalogResponse>(api.GET("/api/meta/metrics")),
    retry: 1,
    staleTime: Number.POSITIVE_INFINITY,
  });
}
