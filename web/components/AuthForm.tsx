"use client";

import { loginProblem, safeRedirectPath, signupProblem } from "@/lib/validation";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

type Mode = "login" | "signup";

interface ModeCopy {
  title: string;
  cta: string;
  endpoint: string;
  altHref: string;
  altLabel: string;
}

const COPY: Record<Mode, ModeCopy> = {
  login: {
    title: "Log in",
    cta: "Log in",
    endpoint: "/api/auth/login",
    altHref: "/signup",
    altLabel: "Need an account? Sign up",
  },
  signup: {
    title: "Create your account",
    cta: "Sign up",
    endpoint: "/api/auth/signup",
    altHref: "/login",
    altLabel: "Already have an account? Log in",
  },
};

export function AuthForm({ mode, next }: { mode: Mode; next?: string | undefined }) {
  const router = useRouter();
  const copy = COPY[mode];
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError(null);

    const problem =
      mode === "login" ? loginProblem(email, password) : signupProblem(email, password);
    if (problem) {
      setError(problem);
      return;
    }

    setSubmitting(true);
    try {
      const res = await fetch(copy.endpoint, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        const data = (await res.json().catch(() => null)) as { error?: string } | null;
        setError(data?.error ?? "Something went wrong. Please try again.");
        setSubmitting(false);
        return;
      }
      if (mode === "signup") {
        router.push("/login?registered=1");
        return;
      }
      router.replace(safeRedirectPath(next));
      router.refresh();
    } catch {
      setError("Network error. Please try again.");
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto mt-12 w-full max-w-sm rounded-lg border border-gray-200 p-6 shadow-sm">
      <h1 className="mb-4 font-semibold text-xl">{copy.title}</h1>
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <div>
          <label htmlFor="email" className="block font-medium text-gray-700 text-sm">
            Email
          </label>
          <input
            id="email"
            name="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-black focus:outline-none"
          />
        </div>
        <div>
          <label htmlFor="password" className="block font-medium text-gray-700 text-sm">
            Password
          </label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-black focus:outline-none"
          />
        </div>
        {error ? (
          <p role="alert" className="text-red-600 text-sm">
            {error}
          </p>
        ) : null}
        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded bg-black px-3 py-2 font-medium text-sm text-white disabled:opacity-50"
        >
          {submitting ? "Working…" : copy.cta}
        </button>
      </form>
      <Link href={copy.altHref} className="mt-4 block text-blue-600 text-sm hover:underline">
        {copy.altLabel}
      </Link>
    </div>
  );
}
