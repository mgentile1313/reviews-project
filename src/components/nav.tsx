"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  { href: "/", label: "Themes", matches: (p: string) => p === "/" || p.startsWith("/themes") },
  { href: "/locations", label: "Locations", matches: (p: string) => p.startsWith("/locations") },
  { href: "/heatmap", label: "Heatmap", matches: (p: string) => p.startsWith("/heatmap") },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <header className="border-b-2 border-green-800/15 bg-background">
      <nav className="mx-auto flex h-14 max-w-7xl items-center justify-between px-6">
        <Link
          href="/"
          className="font-semibold tracking-tight text-green-900 hover:text-green-700 transition-colors"
        >
          Reviews Intelligence
        </Link>
        <div className="flex items-center gap-6 text-sm">
          {links.map((link) => {
            const active = link.matches(pathname);
            return (
              <Link
                key={link.href}
                href={link.href}
                className={`font-medium transition-colors ${
                  active
                    ? "text-green-900 underline decoration-sky-600 decoration-2 underline-offset-8"
                    : "text-muted-foreground hover:text-sky-700"
                }`}
              >
                {link.label}
              </Link>
            );
          })}
        </div>
      </nav>
    </header>
  );
}
