import { apiPublicUrl } from "./config";

export type User = {
  id: string;
  email: string;
  is_superuser: boolean;
  created_at: string;
};

export class AuthError extends Error {}

// Reads the CSRF cookie the API sets alongside the session cookie (deliberately not httpOnly,
// so the browser can echo it back as a header — the "double submit cookie" pattern).
function readCsrfCookie(): string | null {
  const match = document.cookie.match(/(?:^|; )recore_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

// Every auth call goes to the API's own origin with the session cookie attached automatically.
async function authFetch(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(`${apiPublicUrl}${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init.headers },
  });
}

// Throws the API's own error message so callers can show it directly to the user.
async function throwIfNotOk(res: Response): Promise<void> {
  if (res.ok) return;
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  throw new AuthError(body?.detail ?? "Something went wrong.");
}

// Creates an account. Does not sign the user in — call login() next.
export async function signup(email: string, password: string): Promise<void> {
  const res = await authFetch("/api/v1/auth/signup", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  await throwIfNotOk(res);
}

// Authenticates and starts a session. The API also sets the session and CSRF cookies.
export async function login(email: string, password: string): Promise<User> {
  const res = await authFetch("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  await throwIfNotOk(res);
  const body = (await res.json()) as { user: User };
  return body.user;
}

// Ends the current session, reading the CSRF token from its cookie — works after a refresh or
// in a second tab, since (unlike in-memory state) the cookie is shared across both.
export async function logout(): Promise<void> {
  const csrfToken = readCsrfCookie();
  if (!csrfToken) return; // no session cookie means we were never signed in
  const res = await authFetch("/api/v1/auth/logout", {
    method: "POST",
    headers: { "X-CSRF-Token": csrfToken },
  });
  await throwIfNotOk(res);
}

// Returns the current user, or null if there's no active session.
export async function fetchMe(): Promise<User | null> {
  const res = await authFetch("/api/v1/auth/me");
  if (res.status === 401) return null;
  await throwIfNotOk(res);
  return (await res.json()) as User;
}
