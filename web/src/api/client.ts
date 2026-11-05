import createClient from "openapi-fetch";
import type { paths } from "./schema.gen";

// openapi-fetch constructs `new Request(url)` internally, which requires an
// absolute URL — a bare "/" baseUrl works in the browser only because the
// document base resolves it, but fails under jsdom/tests. Use the page origin
// when available (same-origin API in prod and dev via the Vite proxy).
const baseUrl =
  typeof window !== "undefined" && window.location?.origin ? window.location.origin : "/";

// Resolve `fetch` lazily so tests (and any runtime fetch polyfill) that stub
// globalThis.fetch after module load still take effect. openapi-fetch captures
// the `fetch` option at createClient() time, so pass a delegating wrapper.
export const api = createClient<paths>({
  baseUrl,
  fetch: (input: Request, init?: RequestInit) => fetch(input, init),
});
