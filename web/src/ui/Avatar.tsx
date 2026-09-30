import type { CSSProperties } from "react";

// Muted hues so each role reads as distinct without shouting.
const hues = [18, 42, 150, 190, 222, 280, 330];

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
