import { type APIResponse, expect, type Page } from "@playwright/test";

/** Seeded fixture constants — deterministic for `callscore-synth --seed 7`. */
export const AS_OF = "2025-06-30";
export const RUN_ID = "2025-06-30-fee61dc02088";
/** Peer group with the most analysts in the seeded run (4 members). */
export const BUSY_PEER_GROUP = "cap_bucket=small|region=NA|sector=SEC_SOFTWARE";
/** A delisted security in the seeded universe (delisting_date < as_of). */
export const DELISTED_NAME = "Summit Energy";

interface RunListBody {
  data: { run_id: string; status: string; as_of_date: string }[];
}

/** Fetch the newest succeeded run id via the API (same origin as the SPA). */
export async function latestSucceededRunId(page: Page): Promise<string> {
  const resp = await page.request.get("/api/runs", {
    params: { status: "succeeded", limit: 1, order: "desc" },
  });
  expect(resp.ok()).toBeTruthy();
  const body = (await resp.json()) as RunListBody;
  const run = body.data[0];
  expect(run, "expected at least one succeeded run").toBeTruthy();
  return run.run_id;
}

/** Wait until the SPA has written `run_id` into the URL. */
export async function waitForRunIdInUrl(page: Page): Promise<string> {
  await expect(page).toHaveURL(/[?&]run_id=/);
  const url = new URL(page.url());
  const runId = url.searchParams.get("run_id");
  expect(runId).toBeTruthy();
  return runId as string;
}

/** Visible data rows of the first table on the page. */
export function tableRows(page: Page) {
  return page.locator("table tbody tr");
}

/** Assert a JSON API response is OK, returning the parsed body. */
export async function expectOk(resp: APIResponse) {
  expect(resp.ok(), `expected 2xx, got ${resp.status()}: ${await resp.text()}`).toBeTruthy();
  return resp.json();
}
