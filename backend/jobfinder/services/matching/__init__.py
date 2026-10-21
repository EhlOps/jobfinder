"""Matching: one user against the cached jobs, cheapest step first.

    prefilter  hard filters (level, place, pay floor, sponsorship, years asked) and a 0-100 rank, no LLM
    service    match_user: filter, rank, then score the top jobs with Claude within the daily budget
    scoring    the stored score, computed from Claude's per-requirement checks; 'unknown' is neutral
"""
