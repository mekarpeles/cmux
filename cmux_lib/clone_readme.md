# cmux clone context

You were cloned from another agent. Your session name, home dir, and source agent were stated in the message just before this one.

**What cloning means:**
- Your `identity.md` was copied from the original — you share their role as a starting point
- You have a fresh session: no shared conversation history, no shared session ID
- Your home dir is entirely yours — write freely to it

**The source agent's home dir** may contain context worth reading:
- `.cq/issues.db` — their task queue (run `cq issue list --home <source_home>` to inspect)
- Notes, scripts, or workflows they wrote

**Do NOT write to the source agent's home.** Their files belong to their session.

Review your `identity.md`, adapt it if your role diverges, and introduce yourself when ready.
