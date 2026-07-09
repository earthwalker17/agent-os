/**
 * UI polish pass — tiny inline SVG icon set (no dependency, currentColor).
 * Icons are decorative reinforcements next to text labels or inside buttons
 * that carry their own title/aria-label, so they are aria-hidden by default.
 */

interface IconProps {
  size?: number
}

function svgProps(size: number) {
  return {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none' as const,
    stroke: 'currentColor',
    strokeWidth: 1.8,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true as const,
    focusable: false as const,
  }
}

/** Sidebar collapse / expand (panel with a left rail). */
export function IconPanelLeft({ size = 16 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <rect x="3" y="4.5" width="18" height="15" rx="2.5" />
      <line x1="9.5" y1="4.5" x2="9.5" y2="19.5" />
    </svg>
  )
}

/** Settings (sliders). */
export function IconSettings({ size = 16 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <line x1="4" y1="7" x2="20" y2="7" />
      <circle cx="9.5" cy="7" r="2.2" fill="var(--bg-surface, none)" />
      <line x1="4" y1="16" x2="20" y2="16" />
      <circle cx="15" cy="16" r="2.2" fill="var(--bg-surface, none)" />
    </svg>
  )
}

/** Agents (spark). */
export function IconSpark({ size = 16 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <path d="M12 3.5 L14 9.5 L20 11.5 L14 13.5 L12 19.5 L10 13.5 L4 11.5 L10 9.5 Z" />
    </svg>
  )
}

/** Global memory (book). */
export function IconBook({ size = 16 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <path d="M5 4.5h11.5A2.5 2.5 0 0 1 19 7v12.5H6.5A2.5 2.5 0 0 1 4 17V5.5" />
      <path d="M4 17a2.5 2.5 0 0 1 2.5-2.5H19" />
    </svg>
  )
}

/** Expandable-row caret (points right; CSS rotates it when open). */
export function IconChevronRight({ size = 12 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <polyline points="9 5.5, 16 12, 9 18.5" />
    </svg>
  )
}

export function IconPlus({ size = 14 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  )
}

export function IconPencil({ size = 13 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <path d="M16.5 4.5l3 3L8 19l-4 1 1-4L16.5 4.5z" />
    </svg>
  )
}

export function IconX({ size = 13 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <line x1="6" y1="6" x2="18" y2="18" />
      <line x1="18" y1="6" x2="6" y2="18" />
    </svg>
  )
}

/** Composer attach (paperclip). */
export function IconPaperclip({ size = 17 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <path d="M20 11.5l-8.2 8.2a5 5 0 0 1-7-7l8.9-8.9a3.3 3.3 0 0 1 4.7 4.7l-8.9 8.8a1.7 1.7 0 0 1-2.4-2.4L15 7" />
    </svg>
  )
}

/** Composer voice input (microphone). */
export function IconMic({ size = 16 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0" />
      <line x1="12" y1="18" x2="12" y2="21" />
    </svg>
  )
}

/** Stop recording. */
export function IconStop({ size = 14 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <rect x="6.5" y="6.5" width="11" height="11" rx="1.5" fill="currentColor" stroke="none" />
    </svg>
  )
}

/** Composer send (arrow up). */
export function IconArrowUp({ size = 16 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <line x1="12" y1="19" x2="12" y2="5.5" />
      <polyline points="5.5 11.5, 12 5, 18.5 11.5" />
    </svg>
  )
}

/* ------------------------------------------------------------------ */
/* Provider brand marks — filled silhouettes for the Integrations panel.
   All inherit currentColor so the panel's CSS decides the tint per theme. */

function brandProps(size: number, viewBox = '0 0 24 24') {
  return {
    width: size,
    height: size,
    viewBox,
    fill: 'currentColor' as const,
    'aria-hidden': true as const,
    focusable: false as const,
  }
}

/** GitHub octocat mark (octicon silhouette). */
export function BrandGitHub({ size = 16 }: IconProps) {
  return (
    <svg {...brandProps(size, '0 0 16 16')}>
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
    </svg>
  )
}

/** Vercel triangle. */
export function BrandVercel({ size = 16 }: IconProps) {
  return (
    <svg {...brandProps(size)}>
      <path d="M12 4.2 21.7 20.5H2.3L12 4.2z" />
    </svg>
  )
}

/** Supabase bolt. */
export function BrandSupabase({ size = 16 }: IconProps) {
  return (
    <svg {...brandProps(size)}>
      <path d="M13.9 2.1 4.2 13.6c-.5.6-.1 1.5.7 1.5h6.1l-1 6.7c-.1.8.9 1.2 1.4.6l9.7-11.5c.5-.6.1-1.5-.7-1.5h-6.1l1-6.7c.1-.8-.9-1.2-1.4-.6z" />
    </svg>
  )
}

/** Stripe "S" glyph. */
export function BrandStripe({ size = 16 }: IconProps) {
  return (
    <svg {...brandProps(size)}>
      <path d="M13.98 10.02c-1.82-.68-2.82-1.2-2.82-2.02 0-.7.57-1.1 1.6-1.1 1.87 0 3.8.72 5.13 1.37V3.72C16.44 3.15 15 2.8 12.76 2.8 9 2.8 6.5 4.77 6.5 8.05c0 5.12 7.04 4.3 7.04 6.51 0 .83-.72 1.1-1.72 1.1-1.53 0-3.49-.63-5.04-1.47v4.6c1.72.74 3.45 1.05 5.04 1.05 3.85 0 6.5-1.9 6.5-5.22-.02-5.53-7.34-4.55-7.34-6.6z" />
    </svg>
  )
}
