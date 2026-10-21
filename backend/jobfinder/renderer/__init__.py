"""Rung 4.5's browser side: the opt-in Playwright render worker (backend/Dockerfile.renderer).

  browser.py   render_capture and render_list in a sandboxed Chromium
  net.py       the guarded network the page sees
  __main__     `python -m jobfinder.renderer`: the container's entrypoint

Callers never import this package: they enqueue a render_* task and wait for its result through
integrations/render (`request_render`), which also holds the sandbox rules (guard.py) both sides share.
"""
