import { AuthForm } from "@/components/AuthForm";

type SearchParams = Record<string, string | string[] | undefined>;

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const sp = await searchParams;
  const rawNext = sp.next;
  const next = typeof rawNext === "string" ? rawNext : undefined;
  const registered = sp.registered != null;

  return (
    <div>
      {registered ? (
        <p className="mx-auto mb-4 max-w-sm rounded bg-green-50 px-3 py-2 text-green-700 text-sm">
          Account created. Please log in.
        </p>
      ) : null}
      <AuthForm mode="login" next={next} />
    </div>
  );
}
