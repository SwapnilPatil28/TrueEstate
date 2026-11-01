import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "TrueEstate — Property due diligence",
  description:
    "Evidence-led property due diligence, checked against public RERA records.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" data-scroll-behavior="smooth">
      <body>{children}</body>
    </html>
  );
}
