Mirror of vault Knowledge/Capsule Corp - Zenkai Rules.md, body_sha256 63347b80b3c0c53ce703e1b126c47f71c1f88bcc1ba8863e518459c6a3d775e3

## For future agent

This is the canonical text of the Zenkai Rules, Capsule Corp's reliability doctrine (decision rows 454 and 455 in [[Capsule Corp - System design decisions 2026-09-06]], 2026-09-27; name picked by Pratik). It binds every script, contract, agent and controller in [[Capsule Corp - A Proper Software Factory]] and [[Scouter]]. Verbatim mirrors carrying this body's sha256 live at `capsule-corp/docs/ZENKAI.md` and `scouter/docs/ZENKAI.md`. A test in each repo fails if its mirror drifts. Cite items as Z1 to Z8 and never copy the text (see the last section).

## Why

Reliable scripts can be reliably wrong. Scouter's commit path reported "committed" for weeks while nothing landed, and every check passed because every check asked the same code. A contract without loopholes cannot alone guarantee correctness, and wording is never loophole-proof; enforcement is. So each rule names the mechanism that refuses or flags a breach and states what concretely counts as one. A rule that is not yet mechanised says "enforced by review until mechanised" and names the card that mechanises it.

## The rules

**Z1. Intent is a separate artefact.**
- **Obligation:** Ship every script and contract with intent examples: real inputs, each paired with the outcome a human calls right and sourced from Pratik, a spec row or an approved recorded run.
- **Applies to:** scripts, contracts.
- **Enforcer:** enforced by review until mechanised by t_450535e2 (intent-examples format, merge lint, the tick running the examples as a second oracle).
- **Violation:** a script or contract merged without intent examples; an example with no named source; a contract pass reported as a pass while an intent example fails (that is a contradiction event).
- **Evidence:** the intent-examples file beside the script or contract, each example citing its source, and the latest run log of those examples.

**Z2. Never one oracle.**
- **Obligation:** Observe every checked fact through two oracles that meet the second-oracle definition below (for example sidecar + database, diff + outputs, parameter delta + image statistic).
- **Applies to:** scripts, contracts, agents (validators, evaluators, reviewers).
- **Enforcer:** review: a check with one oracle is an Important finding and blocks merge; enforced by review until mechanised by t_450535e2.
- **Violation:** a check whose verdict reads the same code path or data source that produced the fact; a status ("done", "committed", "applied") confirmed only by the component that set it.
- **Evidence:** the check names both oracles, and its result records both observations.

**Z3. Precedence decides ties.**
- **Obligation:** Resolve every contradiction by the precedence list below; when the spec contradicts a contract, rewrite the contract through a decision row without a human tie-break; send Pratik only spec-silent or spec-ambiguous cases, with a provisional ruling already applied when it is reversible.
- **Applies to:** contracts, agents, controllers.
- **Enforcer:** the guide role files the contract rewrite (diff card plus decision row); enforced by review until mechanised by t_450535e2 (contradiction events).
- **Violation:** acting on a lower-ranked source over a higher-ranked one; asking Pratik about a case the spec decides; a known contradiction with no decision row; a reversible case left waiting on Pratik with no provisional ruling.
- **Evidence:** one decision row per contradiction, naming both sources and the rank that won.

**Z4. Exceptions expire.**
- **Obligation:** Bypass a rule, check or contract only through an override that meets every point of the override rules below.
- **Applies to:** scripts, agents, controllers.
- **Enforcer:** the `override` verb refuses an override missing any field, past its TTL, repeated, or irreversible without Pratik; enforced by review until mechanised by t_f7f87edd.
- **Violation:** any bypass (skip, xfail, disabled check, force flag, merge over a failing gate) without a live override; an expired override still in effect; a second override of the same rule on the same subject without a contract change.
- **Evidence:** the override record (reason, rule named, fix card, expiry, Pratik's approval when irreversible) and its line in the weekly introspection.

**Z5. Contradiction detectors always run.**
- **Obligation:** Run metamorphic checks, a canary gauntlet slice after every merge, drift alarms on metrics that move without a contract change, and a weekly introspection that ranks contracts by contradiction count.
- **Applies to:** scripts, controllers.
- **Enforcer:** the tick runs the canary slice after each merge once t_c9cf359b lands; enforced by review until mechanised by t_c9cf359b; the weekly introspection checks compliance.
- **Violation:** a merge with no canary run after it; a detector disabled, skipped or failing without an alert; a metric that moved without a contract change and raised no alarm; an introspection without the contradiction ranking.
- **Evidence:** a canary run record per merge commit; the alarm log; the ranking table in the introspection note.

**Z6. Autonomy follows reversibility, not confidence.**
- **Obligation:** Decide and record a decision row yourself when the choice is reversible and inside the spec; ask Pratik and wait for his recorded answer when it is irreversible, product-scope or spec-ambiguous.
- **Applies to:** agents, controllers.
- **Enforcer:** protected paths (workers cannot write contracts, SOUL, CLAUDE.md or `docs/ZENKAI.md`); the override verb once t_f7f87edd lands; review of decision rows until then.
- **Violation:** an irreversible, product-scope or spec-ambiguous action taken without Pratik's recorded answer; a reversible in-spec choice left waiting on Pratik; a decision with no row; confidence given as the reason to act.
- **Evidence:** a decision row stating "reversible" or "irreversible" and, when irreversible, Pratik's answer.

**Z7. Less code.**
- **Obligation:** Build the smallest change that meets the done-when; report `net_lines: +N/-M` for production files, and above +150 state what could have been deleted; comments carry one sentence of why, never review history; reviewers answer "what could be deleted" in every review; one card per month must reduce production lines.
- **Applies to:** implementers, reviewers, controllers, plans.
- **Evidence:** the `net_lines` line in the report; the size oracle in the merge gate (`tests/size_check.py`: complexity 15, function 60 lines, file 800 lines, baseline may only shrink); the reviewer's deletion answer.
- **Violation:** a report without `net_lines`; a new size violation landing on main; a baseline that grows; a comment that narrates a fix round or a finding; a month with no shrinking card.
- **Source:** decision row 509, code simplicity audit 2026-09-27.

**Z8. Simple is complex enough.**
- **Preamble (Pratik, 2026-09-29, row 541):** "Simple is complex enough, because when we go ahead and scale it, it becomes very impossible to figure out complex things. I really believe in doing simple things." And: "It's not slop code or something. It's actually right, good working code with all the software engineering principles."
- **Obligation:** Every artefact the factory writes (code, tests, specs, contracts) is the simplest thing that meets its done-when. Simple means four things; when they conflict, the earlier wins: (1) readable in two months: a cold reader can follow it, with plain names, flat control flow, no tricks; (2) few moving parts: no layer, class, function, wrapper or config option without two real callers or a done-when line that needs it; (3) boring, known tools: stdlib and the approved-tools list first, anything new needs a decision row; (4) fewest lines: Z7, now subordinate to 1-3, since a shorter version that is harder to read is not simpler.
- **Applies to:** implementers, reviewers, controllers, plans, specs, contracts.
- **Enforcer:** `tests/simplicity_check.py` and the tightened limits in `tests/size_check.py` in the merge gate, plus a reviewer checklist whose every line must be answered.
- **Violation:** a merged artefact that breaks a measured rule without a live Z4 override; a review that leaves a checklist line unanswered.
- **Evidence:** the gate's `simplicity_check:` and `size_check:` lines, the verdict's `simplicity` answers, the calibration section of the simplicity plan.
- **Ratchet:** code that predates Z8 is baselined and may never get worse; the monthly simplification card (Z7, row 509) rewrites the worst baseline entries; specs and contracts get rule 1 through the reviewer checklist.
- **Source:** decision row 559 (spec approved 2026-09-29), row 541; text approved as is by Pratik 2026-10-20.

## Precedence

intent rows > spec > contract > script > code. The higher source wins. A contract contradicted by the spec is wrong by definition.

## Definitions

- **Reversible:** can be undone by one recorded command within the same day, without loss of user data and without external side effects. Everything else is irreversible.
- **Second oracle:** an observation that shares no code path and no data source with the first.

## Override rules

1. It states a reason.
2. It names the rule, check or contract it bypasses.
3. It expires 72 h after it is recorded; renewing it is a new override.
4. It links a fix card that removes the need for it.
5. The weekly introspection reviews it.
6. The same override twice (same rule, same subject) forces a contract change; there is no third override.
7. An irreversible override needs Pratik's recorded approval.

## How agents must reference this

- Never copy this text into a brief, prompt, contract or note. Cite it by name and item numbers ("Zenkai Rules Z2, Z4") with the path `docs/ZENKAI.md` in a repo or this note in the vault.
- Workers may not edit this note or its mirrors. `docs/ZENKAI.md` is a protected path in capsule-corp: a run that touches it is restored and failed.
- Only a controller changes it, after a decision row approved by Pratik, and updates `body_sha256` and both mirrors in the same change.

Zenkai Rules v2 (2026-10-20)
