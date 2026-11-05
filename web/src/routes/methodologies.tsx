import { createFileRoute } from "@tanstack/react-router";
import { MethodologiesPage } from "../features/methodologies/MethodologiesPage";

interface MethodologiesSearch {
  fp?: string;
  a?: string;
  b?: string;
}

export const Route = createFileRoute("/methodologies")({
  validateSearch: (search: Record<string, unknown>): MethodologiesSearch => ({
    fp: typeof search.fp === "string" ? search.fp : undefined,
    a: typeof search.a === "string" ? search.a : undefined,
    b: typeof search.b === "string" ? search.b : undefined,
  }),
  component: MethodologiesPage,
});
