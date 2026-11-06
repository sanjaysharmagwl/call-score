import { vi } from "vitest";

// jsdom doesn't implement window.scrollTo; TanStack Router's scroll
// restoration calls it on every navigation.
window.scrollTo = vi.fn();
