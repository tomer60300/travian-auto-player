---
name: provision
description: Execute multi-step infrastructure provisioning with state tracking and verification
---

## Provisioning Framework

For any multi-step provisioning task:

1. **CREATE** a `provision-state.json` tracking file in the project root:
   ```json
   {
     "task": "description of what's being provisioned",
     "started": "ISO timestamp",
     "steps": ["step1", "step2", ...],
     "completed": [],
     "current": null,
     "blocked": [],
     "evidence": {}
   }
   ```

2. **For EACH step:**
   a. Update state to mark step as "in-progress"
   b. Execute the step
   c. **VERIFY** the step succeeded (check service status, test connectivity, validate config)
   d. Update state with result and evidence of success
   e. If step fails, mark as "blocked" with error details and **STOP**

3. **After ALL steps:** run integration verification
   - Test service connectivity
   - Validate configurations
   - Check logs for errors
   - Update provision-state.json with final status

4. **NEVER** skip verification. **NEVER** mark a step complete without evidence.

## Reboot Handling

If a step requires a reboot:
- Save full state to provision-state.json
- Document exactly what to run after reboot
- On resume: read state file, verify pre-reboot steps still hold, continue

## Rollback

If a critical step fails:
- Check if previous steps created reversible state
- Document what was changed and how to undo it
- Do NOT attempt automatic rollback without user confirmation

## Evidence Format

For each completed step, record in the evidence object:
```json
{
  "step_name": {
    "command": "what was executed",
    "output": "truncated output proving success",
    "verified_by": "how success was confirmed",
    "timestamp": "ISO timestamp"
  }
}
```
