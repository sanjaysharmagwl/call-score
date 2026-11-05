import { createRootRoute, Link, Outlet } from "@tanstack/react-router";
import { RunSelector } from "../components/RunSelector";
import { useHealth } from "../features/shared/queries";
import { useActiveRun, useClearStaleRunQueries } from "../features/shared/run-state";

const NAV: { num: string; label: string; to?: string }[] = [
  { num: "01", label: "Overview", to: "/" },
  { num: "02", label: "Leaderboard", to: "/leaderboard" },
  { num: "03", label: "Analysts", to: "/analysts" },
  { num: "04", label: "Securities", to: "/securities" },
  { num: "05", label: "Sectors", to: "/sectors" },
  { num: "06", label: "Change log", to: "/changes" },
];

const LAB_NAV: { label: string; to: string }[] = [
  { label: "Methodology", to: "/methodologies" },
  { label: "Scenarios", to: "/scenarios" },
];

function HealthDot() {
  const { data, isError } = useHealth();
  const ok = !isError && data?.status === "ok";
  return (
    <span
      data-testid="health-dot"
      title={ok ? `API ok (v${data?.version ?? "?"})` : "API unreachable"}
      className="inline-block h-2 w-2 rounded-full"
      style={{ background: ok ? "#4c9a6c" : "var(--color-neutral-400)" }}
    />
  );
}

function Shell() {
  const { runId } = useActiveRun();
  useClearStaleRunQueries(runId);

  return (
    <div className="flex min-h-screen min-w-[1320px] flex-col">
      <header className="flex h-[50px] items-center gap-3 border-b border-(--color-divider) px-4">
        <span className="h-[14px] w-[14px] rounded-(--radius-sm) bg-(--color-accent)" />
        <span className="font-heading text-lg font-semibold tracking-[0.12em]">CALL SCORE</span>
        <span className="mx-2 h-5 w-px bg-(--color-divider)" />
        <span className="text-sm text-(--color-neutral-600)">Analyst rating performance</span>
        <span className="ml-auto flex items-center gap-4">
          <RunSelector />
          <span className="flex items-center gap-2 text-xs text-(--color-neutral-600)">
            <HealthDot />
            API
          </span>
        </span>
      </header>
      <div className="flex flex-1">
        <nav className="w-[200px] shrink-0 border-r border-(--color-divider) bg-(--color-surface) p-3">
          <ul className="flex flex-col gap-1">
            {NAV.map((item) => (
              <li key={item.num} className="flex items-baseline gap-2 text-sm">
                <span className="font-heading text-xs text-(--color-neutral-500)">{item.num}</span>
                {item.to ? (
                  <Link
                    to={item.to}
                    className="text-(--color-text) hover:text-(--color-accent)"
                    activeProps={{ className: "font-medium text-(--color-accent)" }}
                  >
                    {item.label}
                  </Link>
                ) : (
                  <span className="text-(--color-neutral-500)">{item.label}</span>
                )}
              </li>
            ))}
            <li className="mt-3 border-t border-(--color-divider) pt-2">
              <div className="mb-1 text-xs text-(--color-neutral-500)">Lab</div>
              <ul className="flex flex-col gap-1">
                {LAB_NAV.map((item) => (
                  <li key={item.to} className="flex items-baseline gap-2 text-sm">
                    <Link
                      to={item.to}
                      className="text-(--color-text) hover:text-(--color-accent)"
                      activeProps={{ className: "font-medium text-(--color-accent)" }}
                    >
                      {item.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </li>
          </ul>
        </nav>
        <main className="flex-1 p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

export const Route = createRootRoute({
  validateSearch: (search: Record<string, unknown>) => ({
    run_id: typeof search.run_id === "string" ? search.run_id : undefined,
  }),
  component: Shell,
});
