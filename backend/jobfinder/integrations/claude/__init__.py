"""The Claude integration: the headless CLI client, the admin-set token, and one typed task per AI feature.

    claude_code   runs `claude -p` with no tools and validates its output (ClaudeCode, AIError)
    gate          the cluster-wide Claude call permits in valkey, priority lane first (ClaudeGate)
    credentials   the token saved from the admin page
    schemas       the pydantic models each task returns (their JSON schema goes to `claude --json-schema`)
    tasks         AITasks: extract_profile, match, cover_letter, ..., each with its prompt in prompts/

A process takes its AITasks from the context (`ctx.claude`, wired in jobfinder/context.py).
"""
