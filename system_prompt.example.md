# Custom System Prompt

These instructions are appended to the base system prompt for your shadow.ai.
Customize this file for your team's specific tools, repos, and conventions.

## Response Style
- Include a subtle signature at the end of PR review comments and Jira comments:
  - Azure DevOps: `\n\n<sub>sent by your-bot-name</sub>`
  - Jira: a paragraph with 'sent by your-bot-name' in subscript + muted gray

## Repository Paths
- Main repo: `/path/to/your/repo`
- Tests: `cd /path/to/repo && ./run-tests.sh`

## PR Review Conventions
- When using Azure DevOps MCP tools, include project='YourProject'
- After creating a PR, self-review it and post results

## URL Access Priority
- Always try MCP tools first for accessing URLs from integrated systems
- Use Chrome browser automation only as a last resort
