import { redirect } from "next/navigation";

/** Root sends authenticated users to their library; the middleware bounces
 * unauthenticated users on to /login when /library is requested. */
export default function Home() {
  redirect("/library");
}
