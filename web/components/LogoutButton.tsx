"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

export function LogoutButton() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  async function onLogout(): Promise<void> {
    setBusy(true);
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } finally {
      router.replace("/login");
      router.refresh();
    }
  }

  return (
    <button
      type="button"
      disabled={busy}
      onClick={() => void onLogout()}
      className="text-gray-600 hover:text-black hover:underline disabled:opacity-50"
    >
      Log out
    </button>
  );
}
