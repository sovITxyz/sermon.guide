import { LogoutButton } from "@/components/LogoutButton";
import { SESSION_COOKIE } from "@/lib/session";
import type { Metadata } from "next";
import { cookies } from "next/headers";
import Link from "next/link";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "sermon.guide",
  description: "Theological library + sermon prep.",
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  // Presence check only — drives nav rendering, not authorization (the API and
  // middleware enforce that). Reading the cookie marks the layout dynamic.
  const authed = Boolean((await cookies()).get(SESSION_COOKIE)?.value);

  return (
    <html lang="en">
      <body className="min-h-screen bg-white text-gray-900 antialiased">
        <header className="border-gray-200 border-b">
          <nav className="mx-auto flex max-w-3xl items-center justify-between px-4 py-3">
            <Link href={authed ? "/library" : "/login"} className="font-semibold">
              sermon.guide
            </Link>
            {authed ? (
              <div className="flex items-center gap-4 text-sm">
                <Link href="/search" className="hover:underline">
                  Search
                </Link>
                <Link href="/library" className="hover:underline">
                  Library
                </Link>
                <Link href="/sermons" className="hover:underline">
                  Sermons
                </Link>
                <Link href="/upload" className="hover:underline">
                  Upload
                </Link>
                <LogoutButton />
              </div>
            ) : null}
          </nav>
        </header>
        <main className="mx-auto max-w-3xl px-4 py-8">{children}</main>
      </body>
    </html>
  );
}
