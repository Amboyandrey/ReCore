import type { Metadata } from "next";
import { Manrope, IBM_Plex_Mono } from "next/font/google";
import { AuthProvider } from "@/lib/auth-context";
import { AuthStatus } from "@/components/auth-status";
import "./globals.css";

const manrope = Manrope({
  variable: "--font-manrope",
  subsets: ["latin"],
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  weight: ["400", "500", "600"],
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "ReCore",
  description: "A multi-workspace chat platform that talks to any LLM.",
};

// Shared chrome for every route: fonts, tokens, and the header/footer that frame the app.
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${manrope.variable} ${plexMono.variable}`}>
      <body className="flex min-h-screen flex-col font-sans antialiased">
        <AuthProvider>
          <header className="border-b border-border">
            <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
              <span className="text-lg font-semibold tracking-tight text-text">ReCore</span>
              <AuthStatus />
            </div>
          </header>
          <main className="flex-1">{children}</main>
          <footer className="border-t border-border">
            <div className="mx-auto max-w-5xl px-6 py-4 font-mono text-xs text-text-muted">
              ReCore &middot; multi-workspace LLM chat platform
            </div>
          </footer>
        </AuthProvider>
      </body>
    </html>
  );
}
