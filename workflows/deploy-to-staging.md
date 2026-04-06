---
name: deploy-to-staging
description: Deploy a service branch to the staging environment.
usage: "@bot run deploy-to-staging branch=feature/xyz service=avis env=staging"
parameters:
  - name: branch
    required: true
    description: The branch to deploy
  - name: service
    required: true
    description: The service name (avis, mirage, etc.)
  - name: env
    required: false
    description: Target environment
    default: staging
---

# Deploy to Staging

## Step 1: Verify branch exists
Check that branch `{branch}` exists in the `{service}` repo using Azure DevOps MCP tools.
If the branch doesn't exist, STOP and report the error.

## Step 2: Check latest commit
Show the latest commit on `{branch}` — author, message, date.

## Step 3: Run tests
Check if tests are passing for `{branch}`. Use Azure DevOps MCP to check build status.
If tests are failing, STOP and report the failures.

## Step 4: Deploy
Run the deployment for `{service}` to `{env}`:
```bash
cd /tmp && fik deploy {env} --service {service} --branch {branch}
```
If the deploy command is not available, report what command should be run.

## Step 5: Verify
Check the deployment status. If possible, hit the health endpoint.

## Step 6: Report
Post a summary:
- Service: `{service}`
- Branch: `{branch}`
- Environment: `{env}`
- Test status: pass/fail
- Deploy status: success/failed
- Any issues found
