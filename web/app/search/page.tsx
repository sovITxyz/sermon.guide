import { SearchPanel } from "@/components/SearchPanel";

export default function SearchPage() {
  return (
    <section>
      <h1 className="mb-2 font-semibold text-xl">Search</h1>
      <p className="mb-6 text-gray-600 text-sm">
        Ask a question and get a short grounded summary synthesized from your library, with
        citations back to the passages it drew on.
      </p>
      <SearchPanel />
    </section>
  );
}
