---
name: summarize
description: "Use when the user asks to summarize a conversation, thread, document, or PR. Produces concise, structured summaries."
---

# Summarize

Produce a clear, structured summary.

## Format
- **What**: One-line description of what was discussed/done
- **Key Decisions**: Bullet points of decisions made and their reasoning
- **Actions Taken**: What was actually done (code changes, tickets created, etc.)
- **Open Items**: Anything unresolved or needing follow-up
- **Next Steps**: What should happen next

## Rules
- Lead with the most important information
- Be specific — include ticket IDs, file paths, branch names
- Skip small talk and meta-discussion
- If summarizing code changes, mention the files and what changed
- Keep it under 10 bullet points total
