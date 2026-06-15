#!/usr/bin/env bash
# experiment: coordinator + two specialist agents
#
# Alice coordinates. Bob audits today's GitHub issues on Open Library.
# Carol audits open PRs. Each reports back to Alice via cmux send.
# Alice compiles a combined summary.
#
# All three agents share the "team-demo" tmux workspace as windows.
#
# Usage:   bash examples/team_demo.sh
# Watch:   cmux attach alice   (Ctrl-b n / Ctrl-b p to switch windows)
# Stop:    cmux stop alice && cmux stop bob && cmux stop carol

set -e

WORKSPACE=team-demo
REPO=internetarchive/openlibrary
TODAY=$(date +%Y-%m-%d)

echo "Starting team-demo workspace..."
echo ""

# Alice: coordinator. Waits for Bob and Carol, then compiles.
cmux -s $WORKSPACE start alice -d -- \
  "You are Alice, coordinating a two-person research team. \
Bob is checking GitHub issues created today on the Open Library repository \
and Carol is checking for open pull requests. Both will send you their findings \
via cmux. When you have received reports from both, write a concise combined summary \
(a few bullet points each) and post it as a GitHub gist using the gh CLI. \
Do not start until you have heard from both Bob and Carol."

sleep 3

# Bob: issues created today.
cmux -s $WORKSPACE start bob -d -- \
  "You are Bob, a researcher on a team coordinated by Alice. \
Your job: fetch GitHub issues created today ($TODAY) on $REPO. \
Run this command in bash: \
gh issue list --repo $REPO --state open --json number,title,createdAt,labels --limit 100 \
Then filter to issues where createdAt starts with $TODAY. \
Summarize: total count, titles, and any recurring themes or priority labels. \
Once you have your summary, send it to Alice by running in bash: \
cmux send alice \"<your summary>\" --from bob \
Do this now — do not wait for instructions."

sleep 3

# Carol: open PRs (created or updated today).
cmux -s $WORKSPACE start carol -d -- \
  "You are Carol, a researcher on a team coordinated by Alice. \
Your job: fetch open pull requests on $REPO that were created or updated today ($TODAY). \
Run this command in bash: \
gh pr list --repo $REPO --state open --json number,title,createdAt,updatedAt,author,labels --limit 100 \
Then filter to PRs where createdAt or updatedAt starts with $TODAY. \
Summarize: total count, titles, authors, and any notable labels (e.g. Priority, Needs: Review). \
Once you have your summary, send it to Alice by running in bash: \
cmux send alice \"<your summary>\" --from carol \
Do this now — do not wait for instructions."

echo "All three agents started in workspace '$WORKSPACE'."
echo ""
echo "Watch the team work:"
echo "  cmux attach alice    (Ctrl-b n / Ctrl-b p to move between windows)"
echo ""
echo "When done:"
echo "  cmux stop alice && cmux stop bob && cmux stop carol"
