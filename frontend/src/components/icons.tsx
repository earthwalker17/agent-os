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
