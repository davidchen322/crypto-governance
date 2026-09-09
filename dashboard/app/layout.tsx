import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Crypto Governance Dashboard",
  description: "Phase 8 — governance proposals, chat, and version history",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col bg-gray-50 text-gray-900">
        <header className="border-b bg-white">
          <nav className="mx-auto max-w-5xl px-4 py-3 flex gap-6 text-sm font-medium">
            <Link href="/proposals" className="hover:text-blue-600">
              Proposals
            </Link>
            <Link href="/chat" className="hover:text-blue-600">
              Chat
            </Link>
          </nav>
        </header>
        <main className="flex-1 mx-auto w-full max-w-5xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
