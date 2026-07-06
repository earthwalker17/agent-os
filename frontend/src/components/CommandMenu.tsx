import type { AgentInfo } from '../types'

/** One row of the composer's @-command autocomplete. */
export interface CommandEntry {
  command: string
  name: string
  description: string
  badges: string[]
}

function firstSentence(text: string): string {
  const idx = text.indexOf('. ')
  return idx >= 0 ? text.slice(0, idx + 1) : text
}

/**
 * Phase 10 — derive autocomplete rows from the agent registry: active agents
 * with a chat command, alias commands expanded into their own rows. Badge
 * labels are intentionally coarse (read-only / web / dispatches run /
 * asks first) — the Agents browser holds the full contract.
 */
export function buildCommandEntries(agents: AgentInfo[]): CommandEntry[] {
  const entries: CommandEntry[] = []
  for (const a of agents) {
    if (a.status !== 'active' || !a.command) continue
    const badges: string[] = []
    if (a.capabilities.read_only) badges.push('read-only')
    if (a.capabilities.searches_web) badges.push('web')
    if (a.capabilities.dispatches_runs) badges.push('dispatches run')
    if (a.capabilities.requires_confirmation) badges.push('asks first')
    entries.push({
      command: a.command,
      name: a.name,
      description: firstSentence(a.introduction),
      badges,
    })
    for (const alias of a.aliases) {
      entries.push({
        command: alias,
        name: a.name,
        description: `Same as ${a.command}.`,
        badges,
      })
    }
  }
  return entries
}

interface Props {
  entries: CommandEntry[]
  selectedIndex: number
  onSelect: (entry: CommandEntry) => void
  onHover: (index: number) => void
}

/** Upward-opening popover anchored to the composer (ModelPicker's pattern). */
function CommandMenu({ entries, selectedIndex, onSelect, onHover }: Props) {
  if (entries.length === 0) return null
  return (
    <ul className="command-menu" role="listbox">
      {entries.map((entry, i) => (
        <li key={entry.command} role="option" aria-selected={i === selectedIndex}>
          <button
            type="button"
            className={`command-menu-option${i === selectedIndex ? ' selected' : ''}`}
            // Keep focus in the textarea so selection feels seamless.
            onMouseDown={e => e.preventDefault()}
            onClick={() => onSelect(entry)}
            onMouseEnter={() => onHover(i)}
          >
            <span className="command-menu-top">
              <span className="command-menu-command">{entry.command}</span>
              <span className="command-menu-name">{entry.name}</span>
              {entry.badges.map(b => (
                <span key={b} className={`command-badge badge-${b.replace(/\s+/g, '-')}`}>
                  {b}
                </span>
              ))}
            </span>
            <span className="command-menu-desc">{entry.description}</span>
          </button>
        </li>
      ))}
    </ul>
  )
}

export default CommandMenu
