/// <reference types="next" />
/// <reference types="next/image-types/global" />

// NOTE: This file should not be edited
// see https://nextjs.org/docs/app/api-reference/config/typescript for more information.
//
// `next dev`/`next build` will append a third line:
//   /// <reference path="./.next/types/routes.d.ts" />
// It is intentionally NOT committed — `.next/` is gitignored and CI runs
// `tsc --noEmit` without a build, so that reference would be a missing-file
// error. tsconfig's `include` already globs `.next/types/**/*.ts`, so typed
// routes load locally when present and are simply absent in CI.
