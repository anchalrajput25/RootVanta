import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Rootvanta",
  description: "Finds the cause of an incident, proposes a safe fix, and proves it worked.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
