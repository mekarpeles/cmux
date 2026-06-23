# Agent brain migration guide

This file is placed in your cmux home directory (`~/.cmux/{name}/`) to guide you
through migrating your context. Read it once, follow the checklist, then delete or
archive it when done.

The goal: everything that is *yours* — your identity, your notes, your decisions,
your learnings — should live here alongside your cq queue. Shared team files stay
where they are; you keep references to them.

---

## The distinction that matters

**Copy here** — content that is specific to you as an agent:
- Your identity/role prompt ("You are Saul, the Auth Specialist...")
- Personal notes and decisions you have accumulated
- Your workflow file if it lives in `~/Projects/pm/workflows/` — copy it to
  `~/.cmux/{name}/workflows/` and update your registration:
  `cmux agent register {name} --workflow ~/.cmux/{name}/workflows/your-workflow.md`
- Any scratchpad files, research notes, or context docs you maintain personally

**Leave in place, keep the reference** — shared team content:
- `~/Projects/pm/workflows/` files that multiple agents use or that Mek maintains
- `~/Projects/pm/AGENTS.md` and other team-wide docs
- Any repo you work *on* (e.g. `~/Projects/openlibrary`) — that is your workspace,
  not your brain

---

## Checklist

Work through this yourself. Do not ask another agent or Mek to do it for you —
only you know what you consider important context.

- [ ] **Locate your identity prompt.** Is it in `agents.db` (`cmux agent list` shows
      it), in a file in `~/Projects/pm/`, or only in your session history?
      Copy it to `~/.cmux/{name}/identity.md`.

- [ ] **Locate your workflow file.** Check `cmux agent list` for your `workflow`
      path. If it points to `~/Projects/pm/workflows/`, decide:
      - Is this workflow shared with other agents? → leave it, keep the reference.
      - Is it yours alone? → copy to `~/.cmux/{name}/workflows/`, update registration.

- [ ] **Review personal notes.** Any files you have been writing to in `~/Projects/pm`
      or elsewhere that contain your own notes, decisions, or learnings — copy them
      to `~/.cmux/{name}/notes/`.

- [ ] **Initialize your cq.** Run `cq issue create -t "..." ` for any tasks you
      currently hold mentally or in the cmux task queue. Your cq lives at
      `~/.cmux/{name}/.cq/` automatically when `CMUX_SESSION_NAME` is set.

- [ ] **Verify.** Confirm you can reconstruct your full working context from
      `~/.cmux/{name}/` plus references to shared files. Nothing should be
      exclusively in session history — session history does not survive a restart.

- [ ] **Do not delete originals.** Copies only. Leave everything in `~/Projects/pm`
      exactly as it is. Another agent or Mek may depend on it.

- [ ] **Report back.** Open a cq issue in the *project* queue confirming your
      migration is complete:
      ```
      CQ_STATE_DIR=~/Projects/cmux/.cq cq issue comment 11 \
        -b "{name}: migration complete. Identity in identity.md, workflow at ..., notes copied."
      ```

---

## If you are unsure whether something belongs here

Default to copying it. Storage is cheap. Provenance is valuable. You can always
decide later that something is redundant — you cannot recover something that was
never copied before a session restart wiped it.
