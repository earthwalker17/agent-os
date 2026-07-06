# Failure Triage Method
> Ordered steps from symptom to confirmed cause.

1. State the symptom precisely: what happened, where, when, and the exact error text or wrong output. No interpretation yet.
2. Establish the last known-good state: when did this work, and what changed since (code, config, data, environment, dependencies)?
3. Collect evidence before hypotheses: logs, stack traces, exit codes, timestamps, inputs. Quote them verbatim.
4. Localize: which layer failed first? Distinguish the original fault from downstream noise — the first error in time usually matters most.
5. Form at most two ranked hypotheses, each tied to a specific piece of evidence.
6. Name the single cheapest observation that would confirm or kill the top hypothesis.
7. Conclude with confidence level (confirmed / likely / speculative) and propose exactly ONE bounded next step.

Caveats: never report a cause as confirmed without evidence that discriminates it from the alternatives; if evidence is missing, the next step is to obtain it — not to guess a fix.
