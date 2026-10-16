"""integrations: the code that talks to an outside system, one module or package per system.

    ats          the ATS job-board fetchers, one module per vendor, and their registry
    recipes      the recipe engine for boards no ATS adapter reads, and recipe validation
    directories  public lists of job boards (GitHub lists, HN "Who is hiring?") for board discovery
    robots       robots.txt-aware polite fetching, shared by the fetchers above
    usajobs      the USAJOBS search API
    jobspy       the optional python-jobspy scraper
    claude       the Claude Code CLI client, the typed AI tasks and their prompts
    smtp         outbound email
    github       public GitHub profile summaries

Nothing here decides anything about the app's data; services and handlers call in.
"""
