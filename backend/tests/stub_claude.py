"""Helper to build a fake `claude` executable for subprocess-level tests."""
import json
import stat
from pathlib import Path

SCRIPT = """#!/usr/bin/env python3
import json, os, sys, time
ctl = json.load(open({ctl!r}))
call = {{"argv": sys.argv[1:], "env": dict(os.environ), "stdin": sys.stdin.read()}}
open({out!r}, "a").write(json.dumps(call) + "\\n")
step = ctl["steps"].pop(0) if ctl["steps"] else ctl["steps_default"]
json.dump(ctl, open({ctl!r}, "w"))
time.sleep(step.get("sleep", 0))
sys.stderr.write(step.get("stderr", ""))
sys.stdout.write(step.get("stdout", ""))
sys.exit(step.get("exit", 0))
"""


def envelope(**kw) -> str:
    base = {"type": "result", "is_error": False, "result": "", "usage": {"input_tokens": 10, "output_tokens": 5}}
    return json.dumps({**base, **kw})


class StubClaude:
    def __init__(self, tmp: Path, steps: list[dict]):
        self.path = tmp / "claude"
        self.out = tmp / "calls.jsonl"
        self.ctl = tmp / "ctl.json"
        self.ctl.write_text(json.dumps({"steps": steps, "steps_default": {"stdout": envelope(result="{}")}}))
        self.path.write_text(SCRIPT.format(ctl=str(self.ctl), out=str(self.out)))
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC)

    @property
    def calls(self) -> list[dict]:
        if not self.out.exists():
            return []
        return [json.loads(line) for line in self.out.read_text().splitlines()]
