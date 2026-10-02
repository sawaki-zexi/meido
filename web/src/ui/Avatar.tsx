import type { CSSProperties } from "react";

// Sakura-friendly hues (pink, peach, lavender, mint, sky…) so each role reads as distinct on white.
const hues = [340, 10, 28, 150, 200, 265, 300];

export function roleHue(id: string): number {
  let hash = 0;
  for (const char of id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return hues[hash % hues.length];
}

/** Style that exposes the role's accent hue to CSS as `--role-hue`. */
export const roleStyle = (id: string) => ({ "--role-hue": roleHue(id) }) as CSSProperties;

export function Avatar({ id, name, size = "md" }: { id: string; name: string; size?: "sm" | "md" | "lg" }) {
  const initial = Array.from(name.trim())[0] ?? "?";
  return <span className={`avatar avatar-${size}`} style={roleStyle(id)} aria-hidden="true">{initial}</span>;
}
