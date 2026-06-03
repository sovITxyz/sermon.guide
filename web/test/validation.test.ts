import { describe, expect, it } from "vitest";
import {
  isValidEmail,
  loginProblem,
  passwordProblem,
  safeRedirectPath,
  signupProblem,
} from "../lib/validation";

describe("isValidEmail", () => {
  it("accepts a normal address", () => {
    expect(isValidEmail("a@b.co")).toBe(true);
  });

  it("rejects obvious garbage", () => {
    expect(isValidEmail("nope")).toBe(false);
    expect(isValidEmail("a@b")).toBe(false);
    expect(isValidEmail("a b@c.d")).toBe(false);
    expect(isValidEmail("")).toBe(false);
  });
});

describe("passwordProblem", () => {
  it("requires at least 8 characters", () => {
    expect(passwordProblem("short")).toContain("at least 8");
  });

  it("rejects over 128 characters", () => {
    expect(passwordProblem("x".repeat(129))).toContain("at most 128");
  });

  it("accepts a valid-length password", () => {
    expect(passwordProblem("longenough")).toBeNull();
  });
});

describe("signupProblem / loginProblem", () => {
  it("signup flags a bad email first", () => {
    expect(signupProblem("bad", "longenough")).toContain("valid email");
  });

  it("signup enforces password length", () => {
    expect(signupProblem("a@b.co", "short")).toContain("at least 8");
  });

  it("signup passes a good pair", () => {
    expect(signupProblem("a@b.co", "longenough")).toBeNull();
  });

  it("login accepts any non-empty password (the API decides)", () => {
    expect(loginProblem("a@b.co", "x")).toBeNull();
  });

  it("login still requires a password", () => {
    expect(loginProblem("a@b.co", "")).toContain("password");
  });
});

describe("safeRedirectPath", () => {
  it("allows a root-relative path", () => {
    expect(safeRedirectPath("/upload")).toBe("/upload");
    expect(safeRedirectPath("/library")).toBe("/library");
  });

  it("falls back to /library for undefined or non-rooted input", () => {
    expect(safeRedirectPath(undefined)).toBe("/library");
    expect(safeRedirectPath("library")).toBe("/library");
    expect(safeRedirectPath("https://evil.com")).toBe("/library");
  });

  it("blocks protocol-relative and backslash open-redirect tricks", () => {
    expect(safeRedirectPath("//evil.com")).toBe("/library");
    expect(safeRedirectPath("/\\evil.com")).toBe("/library");
  });
});
