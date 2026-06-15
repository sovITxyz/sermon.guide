import { defineConfig } from "vitest/config";

// Base config shared by both workspace projects (see vitest.workspace.ts).
// Project-specific env/include/plugins live in the workspace file; this keeps
// the shared `resolve`/alias settings in one place.
export default defineConfig({
  resolve: {
    alias: {
      "@": new URL(".", import.meta.url).pathname,
    },
  },
});
