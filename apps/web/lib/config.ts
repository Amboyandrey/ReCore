// Server-only base URL for the API — container-network address inside Docker, localhost outside it.
export const apiInternalUrl = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

// Browser-facing base URL for the API — used once client-side calls exist, from phase 2 onward.
export const apiPublicUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
