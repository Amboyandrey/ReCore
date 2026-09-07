# ReCore web

Next.js 16 (App Router) frontend for ReCore. See [`../../docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md)
for the full plan.

## Develop

```bash
pnpm install
pnpm dev
```

Requires the API running at `API_INTERNAL_URL` (see `.env.example`) — copy it to `.env.local`
and adjust if not using the default `docker compose` setup.

## Conventions

- Design tokens live in `app/globals.css` as CSS custom properties, mapped into Tailwind
  utilities via `@theme inline` (Tailwind v4's CSS-first config — there is no `tailwind.config.js`).
- Server components fetch the API directly via `lib/api.ts`; a typed client generated from the
  API's OpenAPI schema lands in a later phase once there's more surface to type.
