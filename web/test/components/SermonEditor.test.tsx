import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AUTOSAVE_DEBOUNCE_MS, AUTOSAVE_MAX_INTERVAL_MS } from "../../lib/sermon-autosave";
import type { DocumentFull } from "../../lib/types";
import { installFetch, jsonResponse } from "./helpers";

/**
 * SermonEditor (Phase 36 — autosave) tests. TipTap/ProseMirror is mocked: its
 * runtime needs DOM measurement APIs jsdom does not implement, and these tests
 * assert the COMPONENT'S CONTRACT — debounce coalescing, the max-interval save,
 * single-flight (no parallel PATCHes), the 200 base_updated_at adopt, the dirty
 * check, the SaveStatus cycle, the pagehide keepalive flush, and the 409
 * conflict banner + reload — not ProseMirror's own behavior.
 *
 * The fake `useEditor` exposes:
 *  - `getJSON()` returning a MUTABLE doc (`content`) so a test can simulate a
 *    keystroke by mutating it then firing the captured `update` handler;
 *  - `on`/`off` capturing the `update` handler the component subscribes to;
 *  - `commands.setContent` recording a conflict-reload reset;
 *  - `chain().focus().toggleX().run()` recording the last toolbar command.
 *
 * Timing: fake timers + `advanceTimersByTimeAsync` per the reader/autosave
 * precedent — never `waitFor` mixed with fake timers (it deadlocks).
 */

interface FakeEditor {
  getJSON: () => unknown;
  isActive: (name: string, attrs?: Record<string, unknown>) => boolean;
  chain: () => FakeChain;
  on: (event: string, handler: () => void) => void;
  off: (event: string, handler: () => void) => void;
  commands: { setContent: (content: unknown) => void };
  lastCommand: string | null;
  content: unknown;
  setContentCalls: unknown[];
  fireUpdate: () => void;
}
interface FakeChain {
  focus: () => FakeChain;
  toggleBold: () => FakeChain;
  toggleItalic: () => FakeChain;
  toggleHeading: (attrs: { level: number }) => FakeChain;
  toggleBulletList: () => FakeChain;
  toggleOrderedList: () => FakeChain;
  run: () => boolean;
}

let fakeEditor: FakeEditor;

function makeFakeEditor(): FakeEditor {
  const updateHandlers = new Set<() => void>();
  const editor: FakeEditor = {
    content: { type: "doc", content: [{ type: "paragraph" }] },
    getJSON: () => editor.content,
    isActive: () => false,
    lastCommand: null,
    setContentCalls: [],
    on: (event, handler) => {
      if (event === "update") {
        updateHandlers.add(handler);
      }
    },
    off: (event, handler) => {
      if (event === "update") {
        updateHandlers.delete(handler);
      }
    },
    commands: {
      setContent: (content) => {
        editor.content = content;
        editor.setContentCalls.push(content);
      },
    },
    fireUpdate: () => {
      for (const handler of updateHandlers) {
        handler();
      }
    },
    chain() {
      const record = (name: string): FakeChain => {
        editor.lastCommand = name;
        return chain;
      };
      const chain: FakeChain = {
        focus: () => chain,
        toggleBold: () => record("bold"),
        toggleItalic: () => record("italic"),
        toggleHeading: (attrs) => record(`heading${attrs.level}`),
        toggleBulletList: () => record("bulletList"),
        toggleOrderedList: () => record("orderedList"),
        run: () => true,
      };
      return chain;
    },
  };
  return editor;
}

// The editor now imports CitationNode (components/editor/CitationNode.tsx),
// which pulls Node/mergeAttributes/ReactNodeViewRenderer/NodeViewWrapper from
// @tiptap/react. These tests don't exercise the citation node view (that lives
// in CitationNode.test.tsx with a separate, lighter mock), so the mock just
// returns inert stand-ins so the module resolves under the SermonEditor mock.
vi.mock("@tiptap/react", () => ({
  useEditor: () => fakeEditor,
  useEditorState: ({
    selector,
  }: {
    selector: (ctx: { editor: FakeEditor | null }) => unknown;
  }) => selector({ editor: fakeEditor }),
  EditorContent: () => null,
  Node: { create: (config: unknown) => config },
  mergeAttributes: (...args: Record<string, unknown>[]) => Object.assign({}, ...args),
  ReactNodeViewRenderer: (component: unknown) => component,
  NodeViewWrapper: () => null,
}));
vi.mock("@tiptap/starter-kit", () => ({
  default: { configure: () => ({ name: "starterKit" }) },
}));
vi.mock("@tiptap/extension-placeholder", () => ({
  default: { configure: () => ({ name: "placeholder" }) },
}));

// Imported AFTER the mocks are registered (vi.mock is hoisted, but keep the
// import below the mock block for readability).
import { SermonEditor } from "../../components/SermonEditor";

function makeDoc(overrides: Partial<DocumentFull> = {}): DocumentFull {
  return {
    document_id: "doc-1",
    title: "My sermon",
    content: { type: "doc", content: [{ type: "paragraph" }] },
    content_text: "",
    schema_version: 1,
    created_at: "2026-06-15T00:00:00Z",
    updated_at: "2026-06-15T10:00:00Z",
    ...overrides,
  };
}

/** Simulate a content keystroke: mutate the editor buffer, then fire `update`. */
function type(text: string): void {
  fakeEditor.content = {
    type: "doc",
    content: [{ type: "paragraph", content: [{ type: "text", text }] }],
  };
  fakeEditor.fireUpdate();
}

function lastBody(stub: ReturnType<typeof installFetch>): Record<string, unknown> {
  const calls = stub.mock.calls;
  const call = calls[calls.length - 1];
  if (!call) {
    throw new Error("expected at least one fetch call");
  }
  const init = call[1] as RequestInit;
  return JSON.parse(init.body as string) as Record<string, unknown>;
}

beforeEach(() => {
  fakeEditor = makeFakeEditor();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("SermonEditor — toolbar & status", () => {
  it("renders the editable title, the toolbar, and a 'Saved' status (no Save button)", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(<SermonEditor document={makeDoc()} />);

    expect(screen.getByLabelText("Sermon title")).toHaveValue("My sermon");
    expect(screen.getByRole("button", { name: "Bold" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Heading 2" })).toBeInTheDocument();
    // Autosave replaced the explicit Save button.
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    expect(screen.getByText("Saved")).toBeInTheDocument();
  });

  it("a toolbar click runs the matching TipTap command", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(<SermonEditor document={makeDoc()} />);

    fireEvent.click(screen.getByRole("button", { name: "Bold" }));
    expect(fakeEditor.lastCommand).toBe("bold");
    fireEvent.click(screen.getByRole("button", { name: "Heading 3" }));
    expect(fakeEditor.lastCommand).toBe("heading3");
  });
});

describe("SermonEditor — autosave debounce", () => {
  it("PATCHes the whitelisted body after the debounce, cycling status saving -> saved", async () => {
    vi.useFakeTimers();
    const stub = installFetch(() =>
      Promise.resolve(jsonResponse(makeDoc({ updated_at: "2026-06-15T11:00:00Z" }))),
    );
    render(<SermonEditor document={makeDoc()} />);

    act(() => type("hello"));
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    // Nothing fires before the debounce elapses.
    expect(stub).not.toHaveBeenCalled();

    await act(() => vi.advanceTimersByTimeAsync(AUTOSAVE_DEBOUNCE_MS));
    expect(stub).toHaveBeenCalledTimes(1);

    const [url, init] = stub.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/documents/doc-1");
    expect(init.method).toBe("PATCH");
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["base_updated_at", "content", "title"]);
    expect(body.base_updated_at).toBe("2026-06-15T10:00:00Z");
    expect(screen.getByText("Saved")).toBeInTheDocument();
  });

  it("coalesces continuous typing into a single PATCH at the debounce cadence", async () => {
    vi.useFakeTimers();
    const stub = installFetch(() =>
      Promise.resolve(jsonResponse(makeDoc({ updated_at: "2026-06-15T11:00:00Z" }))),
    );
    render(<SermonEditor document={makeDoc()} />);

    // Three keystrokes 1 s apart — each resets the 2 s debounce.
    act(() => type("a"));
    await act(() => vi.advanceTimersByTimeAsync(1000));
    act(() => type("ab"));
    await act(() => vi.advanceTimersByTimeAsync(1000));
    act(() => type("abc"));
    // 3 s of typing, zero PATCHes yet — the debounce never settled.
    expect(stub).not.toHaveBeenCalled();

    await act(() => vi.advanceTimersByTimeAsync(AUTOSAVE_DEBOUNCE_MS));
    expect(stub).toHaveBeenCalledTimes(1);
    expect((lastBody(stub).content as { content: unknown }).content).toEqual([
      { type: "paragraph", content: [{ type: "text", text: "abc" }] },
    ]);
  });

  it("does not PATCH when the buffer is unchanged (dirty check)", async () => {
    vi.useFakeTimers();
    const stub = installFetch(() => Promise.resolve(jsonResponse(makeDoc())));
    render(<SermonEditor document={makeDoc()} />);

    // An `update` event with the SAME content (e.g. a selection-only change).
    act(() => fakeEditor.fireUpdate());
    await act(() => vi.advanceTimersByTimeAsync(AUTOSAVE_DEBOUNCE_MS));
    expect(stub).not.toHaveBeenCalled();
  });
});

describe("SermonEditor — max-interval ceiling", () => {
  it("fires a save at the 15 s ceiling even while typing never pauses", async () => {
    vi.useFakeTimers();
    const stub = installFetch(() =>
      Promise.resolve(jsonResponse(makeDoc({ updated_at: "2026-06-15T11:00:00Z" }))),
    );
    render(<SermonEditor document={makeDoc()} />);

    // A keystroke every 1.5 s (< 2 s debounce) for the full max-interval window
    // — the debounce alone would never settle.
    const step = 1500;
    let elapsed = 0;
    let n = 0;
    while (elapsed < AUTOSAVE_MAX_INTERVAL_MS) {
      act(() => type(`x${n++}`));
      await act(() => vi.advanceTimersByTimeAsync(step));
      elapsed += step;
    }
    // The max-interval save must have fired despite the debounce never settling.
    expect(stub.mock.calls.length).toBeGreaterThanOrEqual(1);
  });
});

describe("SermonEditor — single-flight", () => {
  it("never fires parallel PATCHes; coalesces an edit during a flight into one trailing save", async () => {
    vi.useFakeTimers();
    let resolveFirst: ((r: Response) => void) | null = null;
    let calls = 0;
    const stub = installFetch(() => {
      calls += 1;
      if (calls === 1) {
        // Hold the first PATCH open so a second edit lands mid-flight.
        return new Promise<Response>((resolve) => {
          resolveFirst = resolve;
        });
      }
      return Promise.resolve(jsonResponse(makeDoc({ updated_at: "2026-06-15T12:00:00Z" })));
    });
    render(<SermonEditor document={makeDoc()} />);

    act(() => type("first"));
    await act(() => vi.advanceTimersByTimeAsync(AUTOSAVE_DEBOUNCE_MS));
    expect(stub).toHaveBeenCalledTimes(1); // flight 1 open

    // Edit again while flight 1 is still pending, let the debounce elapse:
    // it must NOT start a parallel PATCH.
    act(() => type("first+second"));
    await act(() => vi.advanceTimersByTimeAsync(AUTOSAVE_DEBOUNCE_MS));
    expect(stub).toHaveBeenCalledTimes(1);

    // Resolve flight 1 -> exactly ONE trailing save drains the coalesced edit.
    await act(async () => {
      resolveFirst?.(jsonResponse(makeDoc({ updated_at: "2026-06-15T11:30:00Z" })));
      await Promise.resolve();
    });
    expect(stub).toHaveBeenCalledTimes(2);
    // The trailing save carried the latest buffer and the adopted base token.
    expect(lastBody(stub).base_updated_at).toBe("2026-06-15T11:30:00Z");
  });

  it("adopts the response updated_at as the next base_updated_at", async () => {
    vi.useFakeTimers();
    const stub = installFetch(() =>
      Promise.resolve(jsonResponse(makeDoc({ updated_at: "2026-06-15T11:00:00Z" }))),
    );
    render(<SermonEditor document={makeDoc()} />);

    act(() => type("one"));
    await act(() => vi.advanceTimersByTimeAsync(AUTOSAVE_DEBOUNCE_MS));
    expect(lastBody(stub).base_updated_at).toBe("2026-06-15T10:00:00Z");

    act(() => type("two"));
    await act(() => vi.advanceTimersByTimeAsync(AUTOSAVE_DEBOUNCE_MS));
    // The second save used the FIRST save's returned updated_at, not the load
    // value — reusing the stale base would manufacture a 409.
    expect(lastBody(stub).base_updated_at).toBe("2026-06-15T11:00:00Z");
  });
});

describe("SermonEditor — conflict (409)", () => {
  it("stops autosaving, shows the banner, and reloads latest on demand", async () => {
    vi.useFakeTimers();
    const latest = makeDoc({
      title: "Reloaded title",
      content: { type: "doc", content: [{ type: "paragraph" }] },
      updated_at: "2026-06-15T13:00:00Z",
    });
    // A PATCH conflicts (409); the reload re-GET (no method) returns the latest.
    const stub = installFetch((_input, init?: RequestInit) =>
      init?.method === "PATCH"
        ? Promise.resolve(jsonResponse({ detail: "stale base_updated_at" }, { status: 409 }))
        : Promise.resolve(jsonResponse(latest)),
    );

    render(<SermonEditor document={makeDoc()} />);

    act(() => type("conflicting edit"));
    await act(() => vi.advanceTimersByTimeAsync(AUTOSAVE_DEBOUNCE_MS));

    // 409 -> conflict banner + a reload affordance; buffer preserved.
    expect(screen.getByRole("alert")).toHaveTextContent(/changed in another tab or device/i);
    const reloadBtn = screen.getByRole("button", { name: "Reload latest" });

    const patchCount = stub.mock.calls.filter(
      (c) => (c[1] as RequestInit | undefined)?.method === "PATCH",
    ).length;

    // Autosave is STOPPED: a further edit + debounce fires no new PATCH.
    act(() => type("more typing after conflict"));
    await act(() => vi.advanceTimersByTimeAsync(AUTOSAVE_MAX_INTERVAL_MS));
    const patchCountAfter = stub.mock.calls.filter(
      (c) => (c[1] as RequestInit | undefined)?.method === "PATCH",
    ).length;
    expect(patchCountAfter).toBe(patchCount);

    // Reload latest -> GET, reset editor + title, resume.
    await act(async () => {
      fireEvent.click(reloadBtn);
      await Promise.resolve();
    });
    expect(fakeEditor.setContentCalls.length).toBe(1);
    expect(screen.getByLabelText("Sermon title")).toHaveValue("Reloaded title");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("SermonEditor — pagehide keepalive flush", () => {
  it("flushes a dirty in-budget buffer with keepalive on pagehide", () => {
    const stub = installFetch(() => Promise.resolve(jsonResponse(makeDoc())));
    render(<SermonEditor document={makeDoc()} />);

    act(() => type("unsaved work"));
    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });

    const keepaliveCall = stub.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.keepalive === true,
    );
    expect(keepaliveCall).toBeDefined();
    const init = keepaliveCall?.[1] as RequestInit;
    expect(init.method).toBe("PATCH");
  });

  it("SKIPS the keepalive flush for an oversize (>64 KB) buffer without throwing", () => {
    const stub = installFetch(() => Promise.resolve(jsonResponse(makeDoc())));
    render(<SermonEditor document={makeDoc()} />);

    // A buffer well past the 64 KB keepalive ceiling.
    act(() => type("z".repeat(70_000)));
    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });

    const keepaliveCall = stub.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.keepalive === true,
    );
    expect(keepaliveCall).toBeUndefined();
  });

  it("does not flush a clean buffer on pagehide", () => {
    const stub = installFetch(() => Promise.resolve(jsonResponse(makeDoc())));
    render(<SermonEditor document={makeDoc()} />);

    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });
    expect(stub).not.toHaveBeenCalled();
  });
});
