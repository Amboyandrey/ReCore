import type { NextConfig } from "next";

// The API origin the browser is allowed to fetch() — same one the client bundle calls, so the
// CSP's connect-src has to name it explicitly rather than assuming 'self' (API and web are
// different origins in every environment this runs in, including local dev).
const apiOrigin = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// One security-headers policy for every HTML/asset response — the API sets its own equivalent
// set (see apps/api/app/core/middleware.py) for its JSON responses; this is the browser-facing
// half of the same checklist item (ARCHITECTURE.md #10).
const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
  {
    key: "Content-Security-Policy",
    value: [
      "default-src 'self'",
      // 'unsafe-inline' here isn't a shortcut: the App Router ships its RSC hydration payload
      // as inline <script> tags on every page, and a strict nonce-based CSP (Next's other
      // documented option) requires opting every single page into dynamic rendering to get a
      // fresh nonce per request — a real architectural cost this app's mix of static and
      // dynamic routes isn't worth paying for. This is Next's own documented "without nonces"
      // recipe, not an ad-hoc weakening.
      "script-src 'self' 'unsafe-inline'",
      "style-src 'self' 'unsafe-inline'",
      // The API origin, and only it, for images a conversation's own attachments serve back.
      `img-src 'self' data: ${apiOrigin}`,
      "font-src 'self' data:",
      `connect-src 'self' ${apiOrigin}`,
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'none'",
    ].join("; "),
  },
];

const nextConfig: NextConfig = {
  // Emits a minimal server bundle (server.js + only the deps it needs) for the Docker image.
  output: "standalone",

  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
