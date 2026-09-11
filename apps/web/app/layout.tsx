import type { Metadata } from "next";
import { Manrope, IBM_Plex_Mono } from "next/font/google";
import { AuthProvider } from "@/lib/auth-context";
import { WorkspaceProvider } from "@/lib/workspace-context";
import { SiteChrome } from "@/components/site-chrome";
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
          <WorkspaceProvider>
            <SiteChrome>{children}</SiteChrome>
          </WorkspaceProvider>
        </AuthProvider>
      </body>
    </html>
  );
}
