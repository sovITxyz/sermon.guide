import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail } from "@/lib/http";
import type { TaskStatus } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy a Celery task-status poll to the API with the bearer from the cookie.
 * `taskId` is URL-encoded before interpolation so a crafted path segment can't
 * escape the `/tasks/{id}` route on the upstream. (The API treats the task_id
 * as the capability; see api/uploads.py.)
 */
export async function GET(
  _req: Request,
  ctx: { params: Promise<{ taskId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const { taskId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/tasks/${encodeURIComponent(taskId)}`, {
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Task lookup failed.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as TaskStatus;
  return NextResponse.json(data);
}
