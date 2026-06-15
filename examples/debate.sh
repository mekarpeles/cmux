#!/usr/bin/env bash
# experiment: two agents debate a question
#
# Alice takes one side. Bob takes the other. Each is told to respond to the
# other's last point with a single short paragraph. You watch the exchange
# live by attaching to either window.
#
# Usage: bash examples/debate.sh "Is remote work better than in-office work?"
#
# Attach:  cmux attach alice   (Ctrl-b n to switch to bob)
# Stop:    cmux stop alice && cmux stop bob

set -e

QUESTION="${1:-Is it better to be a generalist or a specialist?}"
ROUNDS="${2:-3}"

echo "Starting debate: $QUESTION"
echo "Rounds: $ROUNDS"
echo ""

cmux start alice -d -- \
  "You are Alice. You are debating the following question: \"$QUESTION\". \
You will argue in FAVOR of the proposition. Keep each response to one short paragraph. \
Wait for the other side to respond before continuing."

cmux start bob -d -- \
  "You are Bob. You are debating the following question: \"$QUESTION\". \
You will argue AGAINST the proposition. Keep each response to one short paragraph. \
Wait for the other side to respond before continuing."

echo "Agents started. Waiting for them to be ready..."
sleep 8

echo ""
echo "Starting debate — $ROUNDS rounds"
echo "Attach to watch:  cmux attach alice  (Ctrl-b n for bob)"
echo ""

cmux send alice "Make your opening argument." --from moderator

for i in $(seq 1 $ROUNDS); do
  sleep 20
  cmux send bob   "Respond to Alice's last point." --from moderator
  sleep 20
  cmux send alice "Respond to Bob's last point."   --from moderator
done

sleep 20
cmux send alice "Give a one-sentence closing statement." --from moderator
cmux send bob   "Give a one-sentence closing statement." --from moderator

echo ""
echo "Debate complete. Agents are still running."
echo "  cmux attach alice    — read alice's view"
echo "  cmux attach bob      — read bob's view"
echo "  cmux stop alice && cmux stop bob   — clean up"
