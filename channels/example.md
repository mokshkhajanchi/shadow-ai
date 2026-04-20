# Channel Rules Template

Copy this file and rename it to match your channel name (e.g. `my-channel.md`).
The bot loads these rules when auto-replying in the monitored channel.

## When to invoke
Describe in plain English which top-level messages should trigger the bot.
A cheap haiku classifier evaluates every top-level message against this
section and skips the bot if the message doesn't match. This section is
REQUIRED — `@bot monitor #channel` will refuse to activate without it.

Example:
> Invoke for engineering questions, bug reports, or Azure DevOps PR URLs.
> Skip FYIs, status updates, casual chatter, and messages clearly directed
> at a specific teammate who isn't the bot.

## How to answer
- Be concise and direct
- Always check MCP tools for real data before answering

## Domain context
- This channel is about [topic]
- Key repos: [list repos]
- Key tools: [Jira project, Sentry project, etc.]

## Constraints
- Never guess — always verify with tools
- If unsure, say so
