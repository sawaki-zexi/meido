import { useEffect, useState, type ReactNode } from "react";
import type { Role } from "../../api/types";
import { roleStyle } from "../../ui/Avatar";

const glyphs = ["❀", "✦", "☾", "♡", "✧", "❁"];
const motifs = 5;

/** FNV-1a, so a card's look is stable for a role and independent of her name. */
function hash(text: string): number {
  let value = 0x811c9dc5;
  for (const char of text) value = Math.imul(value ^ char.charCodeAt(0), 0x01000193) >>> 0;
  return value;
}

/** Traits that make a role's card her own: a background motif and a corner glyph, derived from her ID. */
export function cardTraits(id: string) {
  const value = hash(id);
  return { motif: value % motifs, glyph: glyphs[(value >>> 8) % glyphs.length] };
}

type Props = { role: Role; number: number; className?: string; children?: ReactNode; onPreviewAvatar?: (role: Role) => void };

/** A role's card: number, glyph, her initial over her motif, name, summary and how she calls you. */
export function RoleCard({ role, number, className = "", children, onPreviewAvatar }: Props) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const { motif, glyph } = cardTraits(role.id);
  const initial = Array.from(role.name.trim())[0] ?? "?";
  useEffect(() => setFailedUrl(null), [role.avatarUrl, role.cardImageUrl]);
  return <div className={`role-card motif-${motif} ${className}`.trim()} style={roleStyle(role.id)}>
    <div className="card-top"><span>No.{String(number).padStart(3, "0")}</span><span aria-hidden="true">{glyph}</span></div>
    <div className="card-art">
      <span className="card-initial" aria-hidden="true">{initial}</span>
      {(role.cardImageUrl ?? role.avatarUrl) && failedUrl !== (role.cardImageUrl ?? role.avatarUrl) && <img className="card-portrait" src={role.cardImageUrl ?? role.avatarUrl!} alt={`${role.name}卡片图片`} onError={() => setFailedUrl(role.cardImageUrl ?? role.avatarUrl!)} />}
      {onPreviewAvatar && (role.avatarOriginalUrl ?? role.avatarUrl) && <button type="button" className="card-portrait-hit" aria-label={`预览${role.name}头像`} onClick={() => onPreviewAvatar(role)} />}
    </div>
    <div className="card-foot">
      <strong>{role.name}</strong>
      <small>{role.description || "暂无简介"}</small>
      {role.profile.nickname && <em>称呼你为「{role.profile.nickname}」</em>}
    </div>
    {children}
  </div>;
}
