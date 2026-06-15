#!/usr/bin/env node
// Phase 43 DOCX round-trip — the React-free Node leg.
//
// Invoked as a subprocess by worker/convert.py:
//   node cli.mjs export   < prosemirror.json  > document.html
//   node cli.mjs import    < document.html      > prosemirror.json
//
// `export` reads ProseMirror/TipTap JSON on stdin and writes the HTML that
// `@tiptap/html` produces (then pandoc turns it into a .docx). `import` reads
// HTML (pandoc's docx->html output) on stdin and writes the ProseMirror JSON
// `@tiptap/html` parses out of it.
//
// EXTENSION PARITY (load-bearing): the extension set MUST match web's
// SermonEditor `buildExtensions`:
//   StarterKit.configure({ link: false }) + CitationNode
// Placeholder is editor-only (a decoration, not a node) and is OMITTED — it
// contributes no schema nodes, so generateHTML/generateJSON are unaffected. If
// the extension sets diverge, generateHTML silently DROPS unknown nodes.
//
// happy-dom: `@tiptap/html/server` is the server-safe path — it constructs its
// OWN local happy-dom `Window` per call (no real browser, no jsdom, NO global
// DOM registration) and aborts/closes it in a finally. happy-dom is therefore a
// mandatory peer dependency of @tiptap/html (pinned in package.json) but this
// CLI never touches it directly.

import { generateHTML, generateJSON } from "@tiptap/html/server";
import StarterKit from "@tiptap/starter-kit";
import { CitationNode } from "./citation-extension.mjs";

// The SAME set web/components/SermonEditor.tsx buildExtensions uses, minus the
// editor-only Placeholder. Link is disabled in StarterKit so the only links
// that round-trip are citation deep-links (rebuilt by CitationNode).
const extensions = [StarterKit.configure({ link: false }), CitationNode];

/** Read all of stdin as a UTF-8 string. */
async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function main() {
  const mode = process.argv[2];
  if (mode !== "export" && mode !== "import") {
    process.stderr.write(`usage: node cli.mjs <export|import>  (got: ${String(mode)})\n`);
    process.exitCode = 2;
    return;
  }

  const input = await readStdin();

  if (mode === "export") {
    // ProseMirror JSON -> HTML.
    let doc;
    try {
      doc = JSON.parse(input);
    } catch (err) {
      process.stderr.write(`export: stdin is not valid JSON: ${String(err)}\n`);
      process.exitCode = 1;
      return;
    }
    const html = generateHTML(doc, extensions);
    process.stdout.write(html);
    return;
  }

  // mode === "import": HTML -> ProseMirror JSON.
  const json = generateJSON(input, extensions);
  process.stdout.write(JSON.stringify(json));
}

main().catch((err) => {
  process.stderr.write(`convert_node failed: ${err && err.stack ? err.stack : String(err)}\n`);
  process.exitCode = 1;
});
