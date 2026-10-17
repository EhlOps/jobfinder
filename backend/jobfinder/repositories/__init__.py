"""repositories: the SQL. Services call these; routers never do.

A repository takes the caller's session and never commits; the boundary does (the request, a
task, the CLI command). One module per table group, each owned by one M4 issue so they never
edit the same file:

    users, profiles, questions             #44
    matches, letters, jobs (reads)         #45
    companies, job_links, coverage         #46 (routers)
    boards, postings (job writes)          #46 (ingestion)
    tasks                                  shared (pending-task lookups)

Need another area's rows? Read the model through `session.get` in your own module; #53 consolidates.
"""
