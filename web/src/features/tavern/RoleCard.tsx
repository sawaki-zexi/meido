import { useEffect, useState, type ReactNode } from "react";
import type { Role } from "../../api/types";
import { Avatar, roleStyle } from "../../ui/Avatar";

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

/** A role's card: number, glyph, portrait, name and summary. */
export function RoleCard({ role, number, className = "", children, onPreviewAvatar }: Props) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const { motif, glyph } = cardTraits(role.id);
  const initial = Array.from(role.name.trim())[0] ?? "?";
  const imageUrl = role.cardImageUrl ?? role.avatarUrl;
  const hasCardImage = Boolean(role.cardImageUrl && failedUrl !== role.cardImageUrl);
  useEffect(() => setFailedUrl(null), [role.avatarUrl, role.cardImageUrl]);
  return <div className={`role-card motif-${motif}${hasCardImage ? " has-card-image" : ""} ${className}`.trim()} style={roleStyle(role.id)}>
    <div className="card-top"><span>No.{String(number).padStart(3, "0")}</span><span aria-hidden="true">{glyph}</span></div>
    <div className="card-art">
      <span className="card-initial" aria-hidden="true">{initial}</span>
      {imageUrl && failedUrl !== imageUrl && <img className="card-portrait" src={imageUrl} alt={`${role.name}卡片图片`} onError={() => setFailedUrl(imageUrl)} />}
      {onPreviewAvatar && !hasCardImage && (role.avatarOriginalUrl ?? role.avatarUrl) && <button type="button" className="card-portrait-hit" aria-label={`预览${role.name}头像`} onClick={() => onPreviewAvatar(role)} />}
    </div>
    {onPreviewAvatar && hasCardImage && (role.avatarOriginalUrl ?? role.avatarUrl) && <button type="button" className="card-avatar-preview" aria-label={`预览${role.name}头像`} onClick={() => onPreviewAvatar(role)}>
      <Avatar id={role.id} name={role.name} avatarUrl={role.avatarUrl ?? role.avatarOriginalUrl} size="sm" />
    </button>}
    <div className="card-foot">
      <strong>{role.name}</strong>
      <small>{role.description || "暂无简介"}</small>
    </div>
    {children}
  </div>;
}
