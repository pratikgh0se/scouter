Mirror of vault Knowledge/Capsule Corp - Zenkai Rules.md, body_sha256 53ef6cd9ef29e62afb80166841ae5a49e7fe4e360a91eb9200ee8cf2b13e5084

## For future agent

This is the canonical text of the Zenkai Rules, Capsule Corp's reliability doctrine (decision rows 454 and 455 in [[Capsule Corp - System design decisions 2026-09-06]], 2026-09-27; name picked by Pratik). It binds every script, contract, agent and controller in [[Capsule Corp - A Proper Software Factory]] and [[Scouter]]. Verbatim mirrors carrying this body's sha256 live at `capsule-corp/docs/ZENKAI.md` and `scouter/docs/ZENKAI.md`. A test in each repo fails if its mirror drifts. Cite items as Z1 to Z6 and never copy the text (see the last section).

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

Zenkai Rules v1 (2026-09-27)
