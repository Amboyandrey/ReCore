import { AdminFlags } from "./admin-flags";

// Superuser-only, and not workspace-scoped — no dynamic route param to resolve here.
export default function AdminFlagsPage() {
  return <AdminFlags />;
}
