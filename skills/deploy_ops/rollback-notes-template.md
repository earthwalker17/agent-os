# Rollback Notes Template
> Recording enough to undo a delivery calmly.

Fill this in at delivery time, not during the incident.

- **What shipped:** one-line description of the change and its intent.
- **When / by whom:** timestamp and who confirmed the delivery.
- **Target:** environment, branch, and the exact version or commit now live.
- **Previous good state:** the commit, tag, or deployment ID to return to — verified to exist.
- **Undo procedure:** ordered steps to restore the previous state, written so a tired person can follow them exactly.
- **Data changes:** migrations or writes that ran; note which are reversible and which need a forward fix instead.
- **Side effects to unwind:** caches, webhooks, cron jobs, third-party settings touched by the release.
- **Verification after rollback:** the 2–3 checks that prove the old version is healthy again.
- **Blast radius if we wait:** who is affected and how urgent the undo is.

Caveat: if any field says "unknown", treat rollback as untested — rehearse it in a staging environment before you need it for real.
