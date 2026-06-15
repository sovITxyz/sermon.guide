import react from "@vitejs/plugin-react";
import { defineWorkspace } from "vitest/config";

// Two projects in one `pnpm test` (Vitest 2.1 workspace):
//  - lib: pure-function unit tests (node env, no DOM) — cookie options,
//    validation, summary segmentation, task-status mapping. These stay
//    node-env and need no React/JSX transform.
//  - components: @testing-library/react component tests (jsdom env) — Phase 25
//    reverses the prior "pure helpers only / no jsdom" posture (see
//    web/AGENTS.md). Uses @vitejs/plugin-react for the JSX transform and a
//    setup file that installs jest-dom matchers + a next/link stub.
export default defineWorkspace([
  {
    extends: "./vitest.config.ts",
    test: {
      name: "lib",
      environment: "node",
      include: ["test/**/*.test.ts"],
    },
  },
  {
    extends: "./vitest.config.ts",
    plugins: [react()],
    test: {
      name: "components",
      environment: "jsdom",
      include: ["test/components/**/*.test.tsx"],
      setupFiles: ["./test/components/setup.ts"],
    },
  },
]);
