import type { ReactNode } from 'react'

/*
  Shared panel chrome. Square corners, 1px borders, a dense uppercase header
  strip. Every panel in the layout uses this so the four regions read as one
  instrument rather than four cards.
*/

interface PanelProps {
  title: string
  /**
   * One line stating what this panel proves. Not a description of the UI —
   * the claim the data on screen supports.
   */
  caption?: string
  /** Right-aligned header content: counts, filters, pagers. */
  aside?: ReactNode
  children: ReactNode
  className?: string
}

export function Panel({ title, caption, aside, children, className = '' }: PanelProps) {
  return (
    <section
      className={`flex min-h-0 min-w-0 flex-col border border-ink-700 bg-ink-900 ${className}`}
    >
      <header className="shrink-0 border-b border-ink-700 bg-ink-850 px-3 pt-1.5 pb-1.5">
        <div className="flex items-center justify-between gap-2">
          <h2 className="shrink-0 t-body font-semibold tracking-[0.12em] text-ink-100 uppercase">
            {title}
          </h2>
          {aside ? (
            <div className="flex items-center gap-2 t-meta text-ink-400">{aside}</div>
          ) : null}
        </div>
        {caption ? (
          <p className="mt-1 t-meta leading-[14px] text-ink-400" title={caption}>
            {caption}
          </p>
        ) : null}
      </header>
      <div className="flex min-h-0 flex-1 flex-col pt-1.5">{children}</div>
    </section>
  )
}

/** Centred low-contrast message for empty and error states. */
export function PanelMessage({
  children,
  tone = 'muted',
}: {
  children: ReactNode
  tone?: 'muted' | 'error'
}) {
  return (
    <div
      className={`flex flex-1 items-center justify-center px-4 text-center t-meta ${
        tone === 'error' ? 'text-state-deny' : 'text-ink-400'
      }`}
    >
      {/* Wrapped in a block: as direct flex children, a <br> between lines is
          itself a flex item and lays out in the row rather than breaking it. */}
      <div>{children}</div>
    </div>
  )
}
