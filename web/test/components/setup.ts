// Component-test setup (jsdom project only — see vitest.workspace.ts).
//  - Installs @testing-library/jest-dom matchers (toBeInTheDocument, …) onto
//    Vitest's `expect`.
//  - Auto-cleans the rendered DOM between tests (RTL no longer does this
//    implicitly under Vitest's globals-off setup).
//  - Stubs `next/link` to a plain <a> so SearchPanel/Uploader render without a
//    Next.js router/app context.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { type ReactNode, createElement } from "react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
});

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: ReactNode }) =>
    createElement("a", { href, ...rest }, children),
}));
