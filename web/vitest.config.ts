import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // Pure-function unit tests only (cookie options, validation, task-status
    // mapping). No DOM/jsdom — components are exercised by the live browser
    // verify, not unit tests, to keep the dependency surface small.
    environment: "node",
    include: ["test/**/*.test.ts"],
  },
});
