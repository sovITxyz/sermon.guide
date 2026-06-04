/**
 * Client-side form validation — UX niceties only. The API is the source of
 * truth (api/auth.py: EmailStr, password 8–128 chars, bcrypt's 72-byte cap),
 * so these are deliberately lenient and never a security boundary.
 */

export const PASSWORD_MIN = 8;
export const PASSWORD_MAX = 128;

export function isValidEmail(email: string): boolean {
  // One `@`, non-empty local part, a dotted domain. The API's EmailStr does the
  // real RFC check; this only catches obvious typos before a round-trip.
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
}

export function passwordProblem(password: string): string | null {
  if (password.length < PASSWORD_MIN) {
    return `Password must be at least ${PASSWORD_MIN} characters.`;
  }
  if (password.length > PASSWORD_MAX) {
    return `Password must be at most ${PASSWORD_MAX} characters.`;
  }
  return null;
}

/** Returns the first problem with a sign-up form, or null if it looks valid. */
export function signupProblem(email: string, password: string): string | null {
  if (!isValidEmail(email)) {
    return "Enter a valid email address.";
  }
  return passwordProblem(password);
}

/** Login is laxer than signup: any non-empty password (the API decides). */
export function loginProblem(email: string, password: string): string | null {
  if (!isValidEmail(email)) {
    return "Enter a valid email address.";
  }
  if (password.length === 0) {
    return "Enter your password.";
  }
  return null;
}

/**
 * Clamp a post-login redirect target to a same-origin, root-relative path.
 * Rejects protocol-relative (`//evil.com`) and backslashed (`/\evil.com`)
 * values a browser resolves to an external origin — an open-redirect guard.
 * Falls back to `/library`.
 */
export function safeRedirectPath(next: string | undefined): string {
  if (next?.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\")) {
    return next;
  }
  return "/library";
}
