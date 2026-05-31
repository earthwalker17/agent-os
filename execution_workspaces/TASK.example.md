# Task State — {name}

## Current Objective
(set by the main agent when delegating; describe the single thing this run
should accomplish)

## Task Queue
- [ ] (queued items the agent should work through, in order)

## In Progress
- [ ] (item currently being worked on; should be empty between runs)

## Completed
- [ ] (items finished in this workspace's lifetime)

## Files Changed
- (list of paths touched, relative to `repo/`)

## Commands Run
- (shell commands executed during runs)

## Blockers
- (anything preventing progress: missing info, unsafe action, failing test
  that needs human judgment)

## Result Summary for Main Agent
(short, factual report of the latest run — overwritten each run)

## Verification

```bash
# Optional: a single shell command run after every Coding Agent run
# finishes. Leave the block empty (or every line commented) to skip
# verification. Lines starting with `#` are ignored; the first
# uncommented line is used as the verify command.
#
# Examples:
#   python -m pytest tests/
#   npm test --silent
#   tsc --noEmit
```

## Browser Verification

```bash
# Optional (Task 06.2B): opt-in headless-browser smoke check.
# Uncomment both lines below to enable for a frontend project.
# Agent OS will start the command, wait for the URL to become
# reachable, take one screenshot, and tear the server back down.
# Leave commented out for backend-only projects.
#
# Example:
#   npm run dev -- --host 127.0.0.1
#   url: http://127.0.0.1:5173
```
