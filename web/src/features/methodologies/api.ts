import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { Schemas } from "../../api/query";
import type { JsonObject, Methodology } from "./fields";

export type MethodologyDefaultsResponse = Schemas["MethodologyDefaultsResponse"];
export type MethodologyVersionSummary = Schemas["MethodologyVersionSummary"];
export type MethodologyDiffResponse = Schemas["MethodologyDiffResponse"];
export type FieldChange = Schemas["FieldChange"];
export type MethodologyListResponse = Schemas["MethodologyListResponse"];

const keys = {
  defaults: () => ["meta", "methodology-defaults"] as const,
  version: (fingerprint: string) => ["methodology", { fingerprint }] as const,
  diff: (a: string, b: string) => ["methodology-diff", { a, b }] as const,
};

async function unwrap<T>(promise: Promise<{ data?: T; error?: unknown }>): Promise<T> {
  const { data, error } = await promise;
  if (error) throw error;
  if (data === undefined) throw new Error("Empty API response");
  return data;
}

/** Extract a human-readable message from an openapi-fetch error body. */
export function formatApiError(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null) {
    const body = error as Record<string, unknown>;
    const detail = body.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const msgs = detail
        .map((d) => {
          if (typeof d === "object" && d !== null) {
            const rec = d as Record<string, unknown>;
            const loc = Array.isArray(rec.loc) ? rec.loc.join(".") : "";
            const msg = typeof rec.msg === "string" ? rec.msg : "";
            return loc ? `${loc}: ${msg}` : msg;
          }
          return String(d);
        })
        .filter(Boolean);
      if (msgs.length > 0) return msgs.join("; ");
    }
    if (typeof body.message === "string") return body.message;
    try {
      return JSON.stringify(error);
    } catch {
      return "Unknown error";
    }
  }
  return String(error);
}

/** `GET /api/meta/methodology-defaults` — built-in presets + default params. */
export function useMethodologyDefaults() {
  return useQuery({
    queryKey: keys.defaults(),
    queryFn: () => unwrap<MethodologyDefaultsResponse>(api.GET("/api/meta/methodology-defaults")),
    retry: 1,
  });
}

/** `GET /api/methodologies/{fingerprint}` — one stored version. */
export function useMethodologyVersion(fingerprint: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.version(fingerprint ?? ""),
    queryFn: () =>
      unwrap<MethodologyVersionSummary>(
        api.GET("/api/methodologies/{fingerprint}", {
          params: { path: { fingerprint: fingerprint ?? "" } },
        }),
      ),
    enabled: enabled && Boolean(fingerprint),
    retry: 1,
  });
}

/** `GET /api/methodologies/diff?a=&b=` — field-level diff of two versions. */
export function useMethodologyDiff(a: string | undefined, b: string | undefined) {
  return useQuery({
    queryKey: keys.diff(a ?? "", b ?? ""),
    queryFn: () =>
      unwrap<MethodologyDiffResponse>(
        api.GET("/api/methodologies/diff", {
          params: { query: { a: a ?? "", b: b ?? "" } },
        }),
      ),
    enabled: Boolean(a && b && a !== b),
    retry: 1,
  });
}

export interface CreateMethodologyInput {
  methodology: Methodology | JsonObject;
  is_baseline?: boolean;
}

/** `POST /api/methodologies` — validate + store an immutable version. */
export function useCreateMethodology() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreateMethodologyInput): Promise<MethodologyVersionSummary> => {
      const { data, error } = await api.POST("/api/methodologies", {
        body: {
          methodology: input.methodology as Methodology,
          is_baseline: input.is_baseline ?? false,
        },
      });
      if (error) throw error;
      if (data === undefined) throw new Error("Empty API response");
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["methodologies"] });
      void queryClient.invalidateQueries({ queryKey: ["methodology"] });
    },
  });
}
