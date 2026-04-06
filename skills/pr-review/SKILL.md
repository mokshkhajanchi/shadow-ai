---
name: pr-review
description: "Use when reviewing a pull request. Fetches PR details, analyzes diff, and provides structured review feedback."
---

# PR Review

## Steps
1. **Fetch PR details** — use Azure DevOps or GitHub MCP tools to get the PR
2. **Get the diff** — fetch changed files and their diffs
3. **Analyze changes** — review each file for issues
4. **Post review** — summarize findings

## Review Categories
- 🔴 **Critical** — Bugs, security issues, data loss risks → must fix
- 🟡 **Warning** — Performance, maintainability, missing tests → should fix
- 🟢 **Suggestion** — Style, naming, minor improvements → nice to have
- ✅ **Good** — Well-written code, good patterns → call it out

## What to Check
- Logic correctness and edge cases
- Error handling — are failures handled gracefully?
- Security — input validation, auth checks, secrets
- Tests — are changes covered? Any missing scenarios?
- Backwards compatibility — will this break existing callers?

## Output Format
Start with a one-line summary, then list issues by severity.
Include file paths and line numbers for every comment.
End with an overall recommendation: approve, request changes, or needs discussion.
