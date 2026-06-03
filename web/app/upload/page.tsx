import { Uploader } from "@/components/Uploader";

export default function UploadPage() {
  return (
    <section>
      <h1 className="mb-2 font-semibold text-xl">Upload a book</h1>
      <p className="mb-6 text-gray-600 text-sm">
        EPUB or PDF. Ingestion runs in the background — watch the status below, then find the book
        in your library.
      </p>
      <Uploader />
    </section>
  );
}
