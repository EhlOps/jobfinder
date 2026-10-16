"""core: the primitives everything else stands on. Read all of it in fifteen minutes.

    config    Settings, from backend/.env.defaults + backend/.env + the environment (no Python defaults)
    db        ORM base, engine, session factory, the per-request session (one transaction per request)
    errors    AppError and its subclasses, and the handler that turns them into responses
    http      the User-Agent and the shared HTTP client
    clock     "now", as something tests can replace
    logging   process log setup
    security  password hashing and random tokens
    context   AppContext: the infrastructure a process shares (jobfinder/context.py adds the integrations)
    urls      hostname and link helpers
    text      tokenizing and whitespace helpers

The rules (tests/test_core_architecture.py enforces the first two):
  1. At most 150 lines per file.
  2. Import nothing from jobfinder outside core/.
  3. No I/O at import time: nothing connects, reads a file or loads settings until it is called.
  4. Fully typed, and the module docstring says what the file is for and when to use it.
  5. Primitives only. Anything that touches models or an outside system (the rate limiter, the
     Claude client, SMTP) belongs elsewhere.
"""
