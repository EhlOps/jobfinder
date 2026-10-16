"""The Claude integration: the headless CLI client, the admin-set token, and one typed task per AI feature.

    claude_code   runs `claude -p` with no tools and validates its output (ClaudeCode, AIError)
    credentials   the token saved from the admin page
    tasks         AITasks: extract_profile, match, cover_letter, ..., each with its prompt in prompts/

A process takes its AITasks from the context (`ctx.claude`, wired in jobfinder/context.py).
"""
