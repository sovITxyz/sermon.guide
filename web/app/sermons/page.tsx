import { NewSermonButton } from "@/components/NewSermonButton";
import { SermonList } from "@/components/SermonList";
import { UnauthenticatedError, getDocuments } from "@/lib/api-server";
import type { DocumentListItem } from "@/lib/types";
import { redirect } from "next/navigation";

export default async function SermonsPage() {
  let documents: DocumentListItem[];
  try {
    documents = await getDocuments();
  } catch (err) {
    if (err instanceof UnauthenticatedError) {
      redirect("/login?next=/sermons");
    }
    throw err;
  }

  return (
    <section>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="font-semibold text-xl">Your sermons</h1>
        <NewSermonButton />
      </div>
      <SermonList documents={documents} />
    </section>
  );
}
