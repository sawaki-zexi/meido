/** Small line icons (24×24, currentColor). Decorative: the button carries the accessible name. */
const paths = {
  bell: 'M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9 M10 21h4 M3 5 1 8 M21 5l2 3',
  monitor: 'M3 3h18v14H3V3Z M8 21h8 M12 17v4',
  building: 'M3 21V8l10-5v18 M13 10h8v11 M1 21h22 M6 10v2 M10 8v2 M6 15v2 M10 13v2 M17 13v2 M17 17v2',
  bot: 'M12 3v3 M10 2h4 M6 7h12a3 3 0 0 1 3 3v9H3v-9a3 3 0 0 1 3-3Z M8 12h.01 M16 12h.01 M9 16h6',
  brain: 'M12 6v14 M12 6a3 3 0 0 0-5-2 4 4 0 0 0-3 6 4 4 0 0 0 1 7 4 4 0 0 0 7 3 M12 6a3 3 0 0 1 5-2 4 4 0 0 1 3 6 4 4 0 0 1-1 7 4 4 0 0 1-7 3 M7 8a3 3 0 0 1 2 3 M4 14a3 3 0 0 1 4 2 M17 8a3 3 0 0 0-2 3 M20 14a3 3 0 0 0-4 2',
  save: 'M4 3h13l4 4v14H3V3h1Z M7 3v6h10V3 M7 21v-8h10v8',
  undo: 'M3 4v6h6 M3 10l5-5a8 8 0 1 1-1 13',
  palette: 'M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 1.5-3.3 1.5 1.5 0 0 1 1.2-2.5H17a4 4 0 0 0 4-4A9 9 0 0 0 12 3Z M7 10h.01 M10 6.5h.01 M15 6.5h.01 M6.5 14h.01',
  sliders: 'M4 7h5 M13 7h7 M4 17h10 M18 17h2 M9 4v6 M14 14v6',
  info: 'M12 11v6 M12 7h.01 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
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
  knowledge: "M12 6v15 M3 4h4a7 7 0 0 1 5 2 7 7 0 0 1 5-2h4v15h-4a7 7 0 0 0-5 2 7 7 0 0 0-5-2H3V4Z M6 8h2 M16 8h2 M6 12h2 M16 12h2",
  mail: "M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Z M3 7l9 6 9-6",
  devices: "M3 6h11v9H3V6Z M6 19h6 M9 15v4 M18 3h3a1 1 0 0 1 1 1v15a1 1 0 0 1-1 1h-3a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z M19 17h1",
  phone: "M8 2h8a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Z M10 5h4 M10 18h4",
  identity: "M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z M9 11a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z M6 16v-1a3 3 0 0 1 6 0v1 M15 9h3 M15 13h3 M15 16h2",
  attachment: "m21 11-9 9a6 6 0 0 1-8.5-8.5l9-9a4 4 0 0 1 5.7 5.7l-9 9a2 2 0 0 1-2.8-2.8L15 6",
  plug: "M8 3v5 M16 3v5 M6 8h12v4a6 6 0 0 1-12 0V8Z M12 18v3",
  image: "M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z M3 17l6-6 5 5 3-3 4 4 M16 7h.01",
  code: "m8 6-6 6 6 6 M16 6l6 6-6 6 M14 3l-4 18",
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
