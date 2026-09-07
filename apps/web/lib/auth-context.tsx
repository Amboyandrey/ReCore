"use client";

import { useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useState } from "react";
import { fetchMe, login as loginRequest, logout as logoutRequest, type User } from "./auth-client";

type AuthContextValue = {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

// Holds the signed-in user for the whole app, refreshing itself once on load from the session cookie.
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchMe()
      .then(setUser)
      .finally(() => setLoading(false));
  }, []);

  async function login(email: string, password: string) {
    setUser(await loginRequest(email, password));
  }

  async function logout() {
    await logoutRequest();
    setUser(null);
  }

  return <AuthContext value={{ user, loading, login, logout }}>{children}</AuthContext>;
}

// Reads the current auth state — must be called under <AuthProvider>.
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}

// Sends the visitor to sign in if, once the initial session check settles, no one's signed in.
export function useRequireAuth(): AuthContextValue {
  const auth = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!auth.loading && !auth.user) router.push("/login");
  }, [auth.loading, auth.user, router]);

  return auth;
}
