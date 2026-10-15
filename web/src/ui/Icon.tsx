/** Small line icons (24×24, currentColor). Decorative: the button carries the accessible name. */
const paths = {
  plus: "M12 5v14M5 12h14",
  stop: "M6 6h12v12H6V6Z",
  people: "M9 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z M2 21v-2a7 7 0 0 1 14 0v2 M17 4a4 4 0 0 1 0 8 M22 21v-2a7 7 0 0 0-4-6.3",
  upload: "M12 15V3M7 8l5-5 5 5 M4 14v6h16v-6",
  grid: "M3 3h7v7H3V3Z M14 3h7v7h-7V3Z M3 14h7v7H3v-7Z M14 14h7v7h-7v-7Z",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z",
  chat: "M21 12a8 8 0 0 1-11.8 7L4 20l1.1-4.6A8 8 0 1 1 21 12Z",
  back: "M15 18l-6-6 6-6",
  profile: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z M4 20a8 8 0 0 1 16 0",
  close: "M6 6l12 12M18 6 6 18",
  cards: "M8 4h10a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z M3 7v11a3 3 0 0 0 3 3 M13 9.5l1.2 2.3 2.3.3-1.7 1.6.4 2.3-2.2-1.2-2.2 1.2.4-2.3-1.7-1.6 2.3-.3L13 9.5Z",
  send: "m21 3-7 18-4-7-7-4L21 3Z M10 14 21 3",
  edit: "M4 20h4L19 9l-4-4L4 16v4Z M13.5 6.5l4 4",
  trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
  memory: "M12 21s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 11c0 5.6-7 10-7 10Z M9.5 12.5h5",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Z M20 20l-4-4",
  model: "M12 3 3 7.5l9 4.5 9-4.5L12 3Z M3 12l9 4.5 9-4.5 M3 16.5 12 21l9-4.5",
  todo: "M8 4h8l2 2h3v15H3V6h3l2-2Z M8 11h8 M8 15h8 M8 18h5",
  refresh: "M20 7v5h-5 M4 17v-5h5 M5.6 9A7 7 0 0 1 18 6l2 2 M4 16l2 2a7 7 0 0 0 12.4-3",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z",
  eyeOff: "M3 3l18 18 M10.6 5.1A12.5 12.5 0 0 1 12 5c6.5 0 10 7 10 7a17.7 17.7 0 0 1-3.1 4.1 M6.2 6.2A19.2 19.2 0 0 0 2 12s3.5 7 10 7a12 12 0 0 0 5.8-1.5 M9.9 9.9a3 3 0 0 0 4.2 4.2",
} as const;

export type IconName = keyof typeof paths;

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  return <svg className="icon-svg" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
    <path d={paths[name]} />
  </svg>;
}

/** Round icon-only button; `label` is both the accessible name and the tooltip. */
export function IconButton({ icon, label, onClick, disabled, className = "" }: { icon: IconName; label: string; onClick: () => void; disabled?: boolean; className?: string }) {
  return <button type="button" className={`icon-button ${className}`.trim()} aria-label={label} title={label} disabled={disabled} onClick={onClick}>
    <Icon name={icon} />
  </button>;
}
