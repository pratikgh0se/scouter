# The HTC merge step's entry point for the test-honesty gate (decision row
# 459, fix round 1). `make gate` just execs scripts/merge_gate.sh, which is
# the thing to actually name from a merge step or a CI job; the target exists
# so a human (or the HTC merge step) never has to remember the script's path.
#
#   make gate BASE=<sha> HEAD=<sha>
#
# Skippable only via SCOUTER_SKIP_MUTATION_CHECK=1 (see scripts/merge_gate.sh
# for why, and how it's logged).
.PHONY: gate
gate:
	@if [ -z "$(BASE)" ] || [ -z "$(HEAD)" ]; then \
		echo "usage: make gate BASE=<sha> HEAD=<sha>" >&2; \
		exit 2; \
	fi
	@scripts/merge_gate.sh "$(BASE)" "$(HEAD)"
