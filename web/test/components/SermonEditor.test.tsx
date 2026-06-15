import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DocumentFull } from "../../lib/types";
import { installFetch, jsonResponse } from "./helpers";

/**
 * SermonEditor (Phase 35) tests. TipTap/ProseMirror is mocked: the editor's
 * ProseMirror runtime needs DOM measurement APIs jsdom does not implement, and
 * these tests assert the COMPONENT'S CONTRACT — the explicit-Save proxy call
 * (right whitelisted body incl. base_updated_at), the 200 base_updated_at
 * advance, and the 409 non-destructive error — not ProseMirror's own behavior.
 *
 * The mock `useEditor` returns a fake editor whose `getJSON()` yields a fixed
 * ProseMirror doc and whose `chain().focus().toggleX().run()` records the last
 * command so a toolbar click is observable. `useEditorState` runs the selector
 * against the same fake editor so the toolbar's active-state path is exercised.
 */

const SAVED_CONTENT = { type: "doc", content: [{ type: "paragraph" }] };

interface FakeEditor {
  getJSON: () => unknown;
  isActive: (name: string, attrs?: Record<string, unknown>) => boolean;
  chain: () => FakeChain;
  lastCommand: string | null;
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
  const editor: FakeEditor = {
    getJSON: () => SAVED_CONTENT,
    isActive: () => false,
    lastCommand: null,
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

vi.mock("@tiptap/react", () => ({
  useEditor: () => fakeEditor,
  // The selector is invoked with { editor } — mirror the real signature.
  useEditorState: ({
    selector,
  }: {
    selector: (ctx: { editor: FakeEditor | null }) => unknown;
  }) => selector({ editor: fakeEditor }),
  EditorContent: () => null,
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

beforeEach(() => {
  fakeEditor = makeFakeEditor();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("SermonEditor", () => {
  it("renders the editable title and the fixed toolbar", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(<SermonEditor document={makeDoc()} />);

    expect(screen.getByLabelText("Sermon title")).toHaveValue("My sermon");
    expect(screen.getByRole("button", { name: "Bold" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Italic" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Heading 2" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Bullet list" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("a toolbar click runs the matching TipTap command", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(<SermonEditor document={makeDoc()} />);

    fireEvent.click(screen.getByRole("button", { name: "Bold" }));
    expect(fakeEditor.lastCommand).toBe("bold");

    fireEvent.click(screen.getByRole("button", { name: "Heading 3" }));
    expect(fakeEditor.lastCommand).toBe("heading3");
  });

  it("Save PATCHes the proxy with the whitelisted body incl. base_updated_at", async () => {
    const fetchStub = installFetch(() =>
      Promise.resolve(jsonResponse(makeDoc({ updated_at: "2026-06-15T11:00:00Z" }))),
    );
    render(<SermonEditor document={makeDoc()} />);

    fireEvent.change(screen.getByLabelText("Sermon title"), {
      target: { value: "Edited title" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    expect(fetchStub).toHaveBeenCalledTimes(1);
    const call = fetchStub.mock.calls[0];
    if (!call) {
      throw new Error("expected a fetch call");
    }
    const [url, init] = call as [string, RequestInit];
    expect(url).toBe("/api/documents/doc-1");
    expect(init.method).toBe("PATCH");
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    // Exactly the three whitelisted fields — no content_text/schema_version/etc.
    expect(Object.keys(body).sort()).toEqual(["base_updated_at", "content", "title"]);
    expect(body.title).toBe("Edited title");
    expect(body.base_updated_at).toBe("2026-06-15T10:00:00Z");
    expect(body.content).toEqual(SAVED_CONTENT);

    expect(await screen.findByText("Saved")).toBeInTheDocument();
  });

  it("after a 200 save, the next save uses the returned updated_at as base", async () => {
    const fetchStub = installFetch(() =>
      Promise.resolve(jsonResponse(makeDoc({ updated_at: "2026-06-15T11:00:00Z" }))),
    );
    render(<SermonEditor document={makeDoc()} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    await screen.findByText("Saved");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    await waitFor(() => expect(fetchStub).toHaveBeenCalledTimes(2));

    const secondCall = fetchStub.mock.calls[1];
    if (!secondCall) {
      throw new Error("expected a second fetch call");
    }
    const [, init] = secondCall as [string, RequestInit];
    const body = JSON.parse(init.body as string) as { base_updated_at: string };
    // Adopted the server's returned updated_at — not the original load value.
    expect(body.base_updated_at).toBe("2026-06-15T11:00:00Z");
  });

  it("surfaces a non-destructive error on a 409 conflict", async () => {
    installFetch(() =>
      Promise.resolve(
        jsonResponse(
          { detail: "Document was modified since base_updated_at; reload and retry." },
          {
            ok: false,
            status: 409,
          },
        ),
      ),
    );
    render(<SermonEditor document={makeDoc()} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/changed in another tab or device/i);
    // Buffer is preserved: the title input still holds the user's edit, not a
    // server reload.
    expect(screen.getByLabelText("Sermon title")).toHaveValue("My sermon");
  });

  it("surfaces a specific message on a 413 (content too large)", async () => {
    installFetch(() =>
      Promise.resolve(jsonResponse({ detail: "too big" }, { ok: false, status: 413 })),
    );
    render(<SermonEditor document={makeDoc()} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(/too large/i);
  });
});
