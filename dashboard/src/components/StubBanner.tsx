import type { Health } from '../api'

/*
  Persistent stub-mode banner.

  Shown whenever the server reports stub_mode. A viewer must never be left to
  infer from context whether they are watching live traffic or replayed
  fixtures — the whole argument of this system is that provenance is stated, not
  assumed. So it is said up front, permanently, before anyone thinks to ask.

  It is not dismissible. A banner you can close is a banner that is closed
  during the demo.
*/

export function StubBanner({ health }: { health: Health | null }) {
  if (!health?.stub_mode) return null

  return (
    <div className="flex shrink-0 items-center gap-2 border border-state-escalate/60 bg-state-escalate/15 px-2 py-1">
      <span className="shrink-0 border border-state-escalate bg-state-escalate px-1 text-[9px] font-bold tracking-[0.1em] text-ink-950">
        STUB MODE
      </span>
      <span className="truncate text-[10px] text-state-escalate">
        Payment legs replayed from recorded Razorpay IDs — no live payment API
        call is made.{' '}
        {/* Named explicitly: "stub mode" does not mean no model ran. The
            upsell agent calls a real provider even here, and a viewer must not
            have to infer which. */}
        {health.llm?.configured ? (
          <>
            Model calls go to{' '}
            <span className="font-semibold">
              {health.llm.provider}/{health.llm.model}
            </span>
            {health.llm.timeout_seconds
              ? ` (${health.llm.timeout_seconds}s timeout)`
              : ''}
            .
          </>
        ) : (
          <>No model is configured; agent steps use recorded fixtures.</>
        )}
      </span>
    </div>
  )
}
