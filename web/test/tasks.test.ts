import { describe, expect, it } from "vitest";
import { isTerminal, taskLabel, taskPhase } from "../lib/tasks";

describe("taskPhase", () => {
  it("maps a fresh success to done", () => {
    expect(taskPhase("SUCCESS", { was_duplicate: false })).toBe("done");
  });

  it("maps a deduplicated success to duplicate", () => {
    expect(taskPhase("SUCCESS", { was_duplicate: true })).toBe("duplicate");
  });

  it("treats success with no result payload as done", () => {
    expect(taskPhase("SUCCESS", null)).toBe("done");
  });

  it("maps failure and revoked to failed", () => {
    expect(taskPhase("FAILURE", null)).toBe("failed");
    expect(taskPhase("REVOKED", null)).toBe("failed");
  });

  it("maps started and retry to running", () => {
    expect(taskPhase("STARTED", null)).toBe("running");
    expect(taskPhase("RETRY", null)).toBe("running");
  });

  it("treats pending and unknown states as pending", () => {
    expect(taskPhase("PENDING", null)).toBe("pending");
    expect(taskPhase("SOMETHING_NEW", null)).toBe("pending");
  });
});

describe("isTerminal", () => {
  it("is true only for terminal states", () => {
    expect(isTerminal("SUCCESS")).toBe(true);
    expect(isTerminal("FAILURE")).toBe(true);
    expect(isTerminal("REVOKED")).toBe(true);
    expect(isTerminal("PENDING")).toBe(false);
    expect(isTerminal("STARTED")).toBe(false);
  });
});

describe("taskLabel", () => {
  it("has a non-empty label for every phase", () => {
    for (const phase of ["pending", "running", "done", "duplicate", "failed"] as const) {
      expect(taskLabel(phase)).toBeTruthy();
    }
  });
});
