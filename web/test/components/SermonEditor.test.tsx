import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AUTOSAVE_DEBOUNCE_MS, AUTOSAVE_MAX_INTERVAL_MS } from "../../lib/sermon-autosave";
import type { DocumentFull, EditorLinkStatus } from "../../lib/types";
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
  setEditable: (editable: boolean) => void;
  isEditable: boolean;
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
    isEditable: true,
    setEditable: (editable) => {
      editor.isEditable = editable;
    },
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

describe("SermonEditor — DOCX round-trip (Phase 43)", () => {
  it("Import overwrites the editor content + title from the API response", async () => {
    const imported = makeDoc({
      title: "Imported title",
      content: { type: "doc", content: [{ type: "paragraph" }] },
      updated_at: "2026-06-15T14:00:00Z",
    });
    const stub = installFetch(() => Promise.resolve(jsonResponse(imported)));
    render(<SermonEditor document={makeDoc()} />);

    const file = new File(["PK docx"], "import.docx", {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    });
    const input = screen.getByLabelText("Word document to import") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
      await Promise.resolve();
    });

    // POSTed to the import proxy with a multipart body (FormData).
    const call = stub.mock.calls.at(-1);
    expect(call?.[0]).toBe("/api/documents/doc-1/import");
    expect((call?.[1] as RequestInit).method).toBe("POST");
    expect((call?.[1] as RequestInit).body).toBeInstanceOf(FormData);

    // The editor adopted the imported doc (setContent + title).
    expect(fakeEditor.setContentCalls.length).toBe(1);
    expect(screen.getByLabelText("Sermon title")).toHaveValue("Imported title");
    // No DOCX error banner on success.
    expect(screen.queryByTestId("docx-error")).not.toBeInTheDocument();
  });

  it("Import surfaces the API's 4xx detail in a visible, dismissable banner", async () => {
    installFetch(() =>
      Promise.resolve(
        jsonResponse(
          { detail: "Unsupported file type. Upload a .docx document." },
          { ok: false, status: 415 },
        ),
      ),
    );
    render(<SermonEditor document={makeDoc()} />);

    const file = new File(["nope"], "reject.docx", {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    });
    const input = screen.getByLabelText("Word document to import") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
      await Promise.resolve();
    });

    const banner = screen.getByTestId("docx-error");
    expect(banner).toHaveTextContent("Unsupported file type");
    // The buffer was NOT clobbered (no setContent on a failed import).
    expect(fakeEditor.setContentCalls.length).toBe(0);

    // Dismiss clears it.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Dismiss error" }));
      await Promise.resolve();
    });
    expect(screen.queryByTestId("docx-error")).not.toBeInTheDocument();
  });

  it("Download triggers a blob download and surfaces an export failure", async () => {
    // First click: a 502 export failure -> visible error, no download triggered.
    const stub = installFetch(() =>
      Promise.resolve(jsonResponse({ detail: "Export failed." }, { ok: false, status: 502 })),
    );
    render(<SermonEditor document={makeDoc()} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Download as Word document" }));
      await Promise.resolve();
    });
    expect(screen.getByTestId("docx-error")).toHaveTextContent("Export failed.");

    // Second click: a successful export. blob() + object-URL plumbing is stubbed
    // (jsdom implements neither); assert the anchor download fires.
    const blob = new Blob(["docx"], {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    });
    const okResponse = {
      ok: true,
      status: 200,
      headers: new Headers({ "content-disposition": 'attachment; filename="My Sermon.docx"' }),
      blob: () => Promise.resolve(blob),
    } as unknown as Response;
    stub.mockImplementation(() => Promise.resolve(okResponse));

    const createObjectURL = vi.fn(() => "blob:fake");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL } as unknown as typeof URL);
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Download as Word document" }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(createObjectURL).toHaveBeenCalledWith(blob);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:fake");
    // The successful export cleared the prior error banner.
    expect(screen.queryByTestId("docx-error")).not.toBeInTheDocument();
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

describe("SermonEditor — external-editor link (Phase 45)", () => {
  const LINKED: EditorLinkStatus = {
    state: "linked",
    web_url: "https://docs.google.com/document/d/abc/edit",
    remote_changed: false,
  };

  it("UNLINKED: editor is editable, no banner, and shows the Link button when Google is connected", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(<SermonEditor document={makeDoc()} googleConnected={true} />);

    expect(fakeEditor.isEditable).toBe(true);
    expect(screen.queryByTestId("editing-externally-banner")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Link to Google Docs" })).toBeInTheDocument();
    // The full formatting toolbar is present when unlinked.
    expect(screen.getByRole("button", { name: "Bold" })).toBeInTheDocument();
  });

  it("UNLINKED + no connection: shows the Connect-Google hint instead of the Link button", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(<SermonEditor document={makeDoc()} googleConnected={false} />);

    expect(screen.queryByRole("button", { name: "Link to Google Docs" })).not.toBeInTheDocument();
    const hint = screen.getByTestId("connect-google-hint");
    expect(hint).toHaveAttribute("href", "/settings/integrations");
  });

  it("LINKED: editor is read-only, banner shows Open/Pull/Unlink, toolbar is hidden", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(<SermonEditor document={makeDoc()} linkStatus={LINKED} googleConnected={true} />);

    // Hard read-only: setEditable(false) ran.
    expect(fakeEditor.isEditable).toBe(false);

    const banner = screen.getByTestId("editing-externally-banner");
    expect(banner).toHaveTextContent("Editing externally in Google Docs");

    // Open is an anchor to web_url with rel="noopener noreferrer" (no token).
    const open = screen.getByRole("link", { name: "Open in Google Docs" });
    expect(open).toHaveAttribute("href", LINKED.web_url);
    expect(open).toHaveAttribute("target", "_blank");
    expect(open).toHaveAttribute("rel", "noopener noreferrer");

    expect(screen.getByRole("button", { name: "Pull changes" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unlink" })).toBeInTheDocument();

    // The formatting toolbar + citation/docx affordances are gone while linked.
    expect(screen.queryByRole("button", { name: "Bold" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Cite from your library" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Download as Word document" }),
    ).not.toBeInTheDocument();
  });

  it("LINKED: a remote change surfaces the Pull-to-update hint", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(
      <SermonEditor
        document={makeDoc()}
        linkStatus={{ ...LINKED, remote_changed: true }}
        googleConnected={true}
      />,
    );
    expect(screen.getByTestId("editing-externally-banner")).toHaveTextContent(
      "Changes available in Google",
    );
  });

  it("LINKED: autosave is hard-suppressed — typing fires no PATCH", async () => {
    vi.useFakeTimers();
    const stub = installFetch(() => Promise.resolve(jsonResponse(makeDoc())));
    render(<SermonEditor document={makeDoc()} linkStatus={LINKED} googleConnected={true} />);

    // Even a content update + the full max-interval window fires NO save while
    // linked (the linked ref gates every scheduler entry point).
    act(() => type("an external edit leaking in"));
    await act(() => vi.advanceTimersByTimeAsync(AUTOSAVE_MAX_INTERVAL_MS));
    const patches = stub.mock.calls.filter(
      (c) => (c[1] as RequestInit | undefined)?.method === "PATCH",
    );
    expect(patches.length).toBe(0);
  });

  it("Link POST flips the editor into read-only linked mode", async () => {
    const stub = installFetch(() => Promise.resolve(jsonResponse(LINKED)));
    render(<SermonEditor document={makeDoc()} googleConnected={true} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Link to Google Docs" }));
      await Promise.resolve();
    });

    const call = stub.mock.calls.at(-1);
    expect(call?.[0]).toBe("/api/documents/doc-1/editor-link");
    expect((call?.[1] as RequestInit).method).toBe("POST");

    expect(fakeEditor.isEditable).toBe(false);
    expect(screen.getByTestId("editing-externally-banner")).toBeInTheDocument();
  });

  it("Pull adopts the returned document into the read-only buffer", async () => {
    const pulled = makeDoc({ title: "Pulled title", updated_at: "2026-06-22T02:00:00Z" });
    const stub = installFetch(() => Promise.resolve(jsonResponse(pulled)));
    render(<SermonEditor document={makeDoc()} linkStatus={LINKED} googleConnected={true} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Pull changes" }));
      await Promise.resolve();
    });

    const call = stub.mock.calls.at(-1);
    expect(call?.[0]).toBe("/api/documents/doc-1/editor-link/pull");
    expect((call?.[1] as RequestInit).method).toBe("POST");
    expect(fakeEditor.setContentCalls.length).toBe(1);
    expect(screen.getByLabelText("Sermon title")).toHaveValue("Pulled title");
    // Still linked + read-only after a pull.
    expect(fakeEditor.isEditable).toBe(false);
  });

  it("Unlink offers BOTH choices; keep-app POSTs {mode:'keep-app'} and returns to editable", async () => {
    const stub = installFetch(() =>
      Promise.resolve(jsonResponse({ state: "unlinked", web_url: null, remote_changed: false })),
    );
    render(<SermonEditor document={makeDoc()} linkStatus={LINKED} googleConnected={true} />);

    // Open the choice dialog.
    fireEvent.click(screen.getByRole("button", { name: "Unlink" }));
    const dialog = screen.getByTestId("unlink-dialog");
    expect(dialog).toBeInTheDocument();
    // Both settled choices are offered.
    expect(screen.getByRole("button", { name: "Pull final copy & unlink" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Keep this version & unlink" })).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Keep this version & unlink" }));
      await Promise.resolve();
    });

    const call = stub.mock.calls.at(-1);
    expect(call?.[0]).toBe("/api/documents/doc-1/editor-link/unlink");
    const body = JSON.parse((call?.[1] as RequestInit).body as string) as Record<string, unknown>;
    expect(body).toEqual({ mode: "keep-app" });

    // Back to editable, banner gone.
    expect(fakeEditor.isEditable).toBe(true);
    expect(screen.queryByTestId("editing-externally-banner")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Bold" })).toBeInTheDocument();
  });

  it("Unlink pull-final POSTs {mode:'pull-final'} then reloads the pulled content", async () => {
    const stub = installFetch((input, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/editor-link/unlink")) {
        return Promise.resolve(
          jsonResponse({ state: "unlinked", web_url: null, remote_changed: false }),
        );
      }
      // The follow-up GET that reloads the final pulled content.
      return Promise.resolve(jsonResponse(makeDoc({ title: "Final pulled" })));
    });
    render(<SermonEditor document={makeDoc()} linkStatus={LINKED} googleConnected={true} />);

    fireEvent.click(screen.getByRole("button", { name: "Unlink" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Pull final copy & unlink" }));
      await Promise.resolve();
      await Promise.resolve();
    });

    const unlinkCall = stub.mock.calls.find((c) => String(c[0]).endsWith("/editor-link/unlink"));
    const body = JSON.parse((unlinkCall?.[1] as RequestInit).body as string) as Record<
      string,
      unknown
    >;
    expect(body).toEqual({ mode: "pull-final" });
    expect(fakeEditor.isEditable).toBe(true);
    expect(screen.getByLabelText("Sermon title")).toHaveValue("Final pulled");
  });

  it("a link failure surfaces the API detail in a dismissable banner", async () => {
    installFetch(() =>
      Promise.resolve(
        jsonResponse(
          { detail: "Document is already linked to an external editor." },
          { ok: false, status: 409 },
        ),
      ),
    );
    render(<SermonEditor document={makeDoc()} googleConnected={true} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Link to Google Docs" }));
      await Promise.resolve();
    });

    const banner = screen.getByTestId("link-error");
    expect(banner).toHaveTextContent("already linked");
    // Still editable (the link never engaged).
    expect(fakeEditor.isEditable).toBe(true);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Dismiss link error" }));
      await Promise.resolve();
    });
    expect(screen.queryByTestId("link-error")).not.toBeInTheDocument();
  });
});
