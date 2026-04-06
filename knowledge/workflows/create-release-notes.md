---
name: create-release-notes
description: Generate release notes from merged PRs since the last release.
parameters:
  - name: version
    required: true
    description: The version being released (e.g., v2.12.0)
  - name: repo
    required: false
    description: Repository name
    default: Commerce
---

# Create Release Notes

## Step 1: Find merged PRs
Use Azure DevOps MCP to list PRs merged into `master` or `version/{version}` since the last release tag.

## Step 2: Categorize changes
Group PRs by type:
- **Features**: new functionality
- **Bug Fixes**: fixes to existing features
- **Tests**: test additions or updates
- **Chores**: refactoring, dependency updates, config changes

## Step 3: Extract Jira tickets
For each PR, extract the Jira ticket ID from the branch name or title.
Use Jira MCP to get the ticket summary.

## Step 4: Generate release notes
Format as:

```
# Release Notes — {version}

## Features
- [FPP-XXXXX] Feature description (PR #NNN)

## Bug Fixes
- [FPP-XXXXX] Fix description (PR #NNN)

## Tests
- [FPP-XXXXX] Test description (PR #NNN)

## Contributors
- @author1, @author2
```

## Step 5: Post
Share the release notes in the thread.
