"""Synthetic Claude Code transcripts for the offline tests. No real conversation content.

Records follow the shapes Claude Code 2.1.322 writes (checked against a live test session on
2026-11-12): one record per content block, blocks of one API message share message.id, parallel
tool calls are consecutive assistant records followed by one user record per tool_result, and a
background task's completion arrives as a non-meta user record whose content starts with
<task-notification>.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

NS = uuid.UUID("6f1c2a52-7d0e-4c55-9a8e-2b1f0d6c9e11")


class Conv:
    def __init__(self, sid, cwd="/tmp/tides-fixture", start="2026-11-01T10:00:00", model="claude-haiku-4-5",
                 recs=None, parent=None, counter=0):
        self.sid, self.cwd, self.model = sid, cwd, model
        self.t = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        self.recs = recs if recs is not None else []
        self.parent = parent
        self.n = counter
        self.usage = 0
        self.tool_n = 0
        self.msg_n = 0

    @classmethod
    def resume(cls, path, start="2026-11-05T10:00:00"):
        """Continue a transcript Tides wrote, the way Claude Code does after the wake."""
        recs = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        leaf = next(r["leafUuid"] for r in reversed(recs) if r.get("type") == "last-prompt")
        sid = Path(path).stem
        c = cls(sid, start=start, recs=recs, parent=leaf, counter=len(recs))
        return c

    # ── plumbing ──
    def _uuid(self):
        self.n += 1
        return str(uuid.uuid5(NS, f"{self.sid}:{self.n}"))

    def _ts(self):
        self.t += timedelta(seconds=7)
        return self.t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self.t.microsecond // 1000:03d}Z"

    def _rec(self, kind, message, **extra):
        r = {"parentUuid": self.parent, "isSidechain": False, "userType": "external", "cwd": self.cwd,
             "sessionId": self.sid, "version": "2.1.322", "gitBranch": "", "type": kind, "message": message,
             "uuid": self._uuid(), "timestamp": self._ts()}
        r.update(extra)
        self.recs.append(r)
        self.parent = r["uuid"]
        return r

    def _usage(self, n):
        self.usage = n if n is not None else self.usage + 900
        return {"input_tokens": 3, "cache_read_input_tokens": self.usage - 103, "cache_creation_input_tokens": 50,
                "output_tokens": 50}

    # ── turns ──
    def user(self, text):
        return self._rec("user", {"role": "user", "content": text})

    def meta(self, text):
        return self._rec("user", {"role": "user", "content": text}, isMeta=True)

    def notification(self, task_id, tool_use_id, status, summary, result=None, output="/tmp/claude-0/x/tasks"):
        body = (f"<task-notification>\n<task-id>{task_id}</task-id>\n<tool-use-id>{tool_use_id}</tool-use-id>\n"
                f"<output-file>{output}/{task_id}.output</output-file>\n<status>{status}</status>\n"
                f"<summary>{summary}</summary>\n" + (f"<result>{result}</result>\n" if result else "")
                + "</task-notification>")
        return self._rec("user", {"role": "user", "content": body},
                         origin={"kind": "task-notification", "producer": "session-task"})

    def assistant(self, blocks, usage=None, stop="end_turn"):
        """One API message: each block becomes its own record, all sharing message.id."""
        self.msg_n += 1
        mid = f"msg_fixture_{self.sid[:4]}_{self.msg_n:04d}"
        u = self._usage(usage)
        out = []
        for b in blocks:
            out.append(self._rec("assistant", {"id": mid, "type": "message", "role": "assistant", "model": self.model,
                                               "content": [b], "stop_reason": stop, "stop_sequence": None,
                                               "usage": dict(u)}, requestId="req_fixture"))
        return out

    def say(self, text, usage=None, thinking=None):
        blocks = ([{"type": "thinking", "thinking": thinking, "signature": "sig"}] if thinking else []) + \
                 [{"type": "text", "text": text}]
        return self.assistant(blocks, usage)

    def tools(self, calls, text=None, usage=None, thinking=None, after=None):
        """One assistant message with optional thinking and text, then the tool_use blocks (parallel when
        more than one), then one user record per result. calls: dicts with name, input, result
        (str | list of blocks | None for no result), error (bool)."""
        blocks = ([{"type": "thinking", "thinking": thinking, "signature": "sig"}] if thinking else []) + \
                 ([{"type": "text", "text": text}] if text else [])
        ids = []
        for c in calls:
            self.tool_n += 1
            tid = f"toolu_fx{self.sid[:4]}{self.tool_n:04d}"
            ids.append(tid)
            blocks.append({"type": "tool_use", "id": tid, "name": c["name"], "input": c["input"]})
        recs = self.assistant(blocks, usage, stop="tool_use")
        src = recs[-1]["uuid"]
        for c, tid in zip(calls, ids):
            if c.get("result") is None:
                continue
            block = {"type": "tool_result", "tool_use_id": tid, "content": c["result"]}
            if c.get("error"):
                block["is_error"] = True
            self._rec("user", {"role": "user", "content": [block]}, toolUseResult={"fixture": True},
                      sourceToolAssistantUUID=src)
        if after:
            self.say(after, usage=self.usage + 300)
        return ids

    def stop_notice(self, n):
        """What Claude Code records when the Tides Stop hook blocks with the curation notice."""
        return self.meta(f"Stop hook feedback:\n🌙 Tides · curation {n}. (fixture notice)")

    def write(self, path):
        with open(path, "w", encoding="utf-8") as f:
            for r in self.recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return path


# ───────────────────────────── the fixtures ─────────────────────────────────

LONG_CMD = "python3 - <<'EOF'\n" + "\n".join(f"print('line {i:03d} of a long heredoc that keeps going')" for i in range(60)) + "\nEOF"
LONG_RESULT = "\n".join(f"result row {i:04d}: value={i * 7 % 101}" for i in range(200))
CONFIG_TEXT = "".join(f"setting_{i} = {i}\n" for i in range(150))
PLANTED = "KESTREL-4471-ZEPHYR"


def tools_dense(sid="11111111-1111-4111-8111-000000000001", cwd="/tmp/tides-fixture"):
    """A never-curated conversation that uses every kind of tool call the digest has a rule for."""
    c = Conv(sid, cwd=cwd)
    c.user("Let's check the build. What is the current build id?")
    c.tools([{"name": "Bash", "input": {"command": "cat build/ID.txt", "description": "Show the build id"},
              "result": f"BUILD-ID: {PLANTED}\n"}],
            text="I'll read the build id file.", thinking="The id lives in build/ID.txt.", usage=10000,
            after="Done: the build id is in the file, as recorded by the command above.")
    c.user("Read the config, fix the typo on setting_7, and write a changelog.")
    c.tools([{"name": "Read", "input": {"file_path": "/tmp/tides-fixture/src/very/deep/nested/package/config/settings_production.py"},
              "result": CONFIG_TEXT}], text="Reading it first.", usage=20000)
    c.tools([{"name": "Edit", "input": {"file_path": "/tmp/tides-fixture/src/config.py", "old_string": "setting_7 = 7",
                                         "new_string": "setting_7 = 8", "replace_all": False},
              "result": "The file /tmp/tides-fixture/src/config.py has been updated successfully."}], usage=21000)
    c.tools([{"name": "Write", "input": {"file_path": "/tmp/tides-fixture/CHANGELOG.md", "content": "# Changes\n" * 80},
              "result": "File created successfully at: /tmp/tides-fixture/CHANGELOG.md"}], usage=22000,
            after="Fixed setting_7 and wrote the changelog.")
    c.user("Search for TODOs and list the test files.")
    c.tools([{"name": "Grep", "input": {"pattern": "TODO|FIXME", "path": "src/", "output_mode": "content"},
              "result": "\n".join(f"src/mod{i}.py:{i}: # TODO item {i}" for i in range(40))},
             {"name": "Glob", "input": {"pattern": "tests/**/*.py"},
              "result": "\n".join(f"tests/test_{i}.py" for i in range(30))}],
            text="Searching both at once.", usage=30000, after="Forty TODOs, thirty test files.")
    c.user("Look up the docs for the parser.")
    c.tools([{"name": "WebFetch", "input": {"url": "https://example.org/docs/parser/reference/v2/all-options-explained-in-detail.html",
                                            "prompt": "List every option"}, "result": LONG_RESULT}], usage=40000)
    c.tools([{"name": "WebSearch", "input": {"query": "parser option strict mode semantics"},
              "result": "1. Strict mode - example.org\n2. Parser FAQ - example.org"}], usage=41000,
            after="The docs list the options; strict mode is documented.")
    c.user("Ask an agent to review it, and load the charts skill.")
    c.tools([{"name": "Agent", "input": {"description": "Review the parser", "subagent_type": "general-purpose",
                                         "prompt": "Review src/parser.py for bugs. " * 30},
              "result": [{"type": "text", "text": "Review: two bugs. " + LONG_RESULT[:900]}]},
             {"name": "Skill", "input": {"skill": "tufte", "args": "bar chart"}, "result": "Launching skill: tufte"}],
            usage=50000, after="The agent found two bugs.")
    c.user("Take a screenshot of the page and update the notebook.")
    c.tools([{"name": "mcp__claude-in-chrome__computer", "input": {"action": "screenshot", "tabId": 12},
              "result": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
                         {"type": "text", "text": "Screenshot taken of tab 12"}]},
             {"name": "NotebookEdit", "input": {"notebook_path": "/tmp/tides-fixture/analysis.ipynb", "cell_id": "c3",
                                                "new_source": "print(1)"}, "result": "Updated cell c3"}],
            usage=60000)       # no assistant text in this turn at all
    c.user("Run the failing script.")
    c.tools([{"name": "Bash", "input": {"command": "python3 missing.py"},
              "result": "python3: can't open file '/tmp/tides-fixture/missing.py': [Errno 2] No such file or directory",
              "error": True}], text="Running it.", usage=70000, after="It failed: the file is missing.")
    c.notification("a1b2c3d4e5f6a7b8c", "toolu_fx1111bg01", "completed", 'Agent "background check" finished',
                   result="The background check found nothing wrong.")
    c.say("The background agent reported back: nothing wrong.", usage=75000)
    c.user("Now the long one.")
    c.tools([{"name": "Bash", "input": {"command": LONG_CMD, "description": "Print sixty lines from a heredoc"},
              "result": LONG_RESULT}], text="Running the long command.", usage=80000, after="Printed sixty lines.")
    # ── from here on: the newest section, which stays whole ──
    c.user("Next: check the disk.")
    c.tools([{"name": "Bash", "input": {"command": "df -h /"}, "result": "Filesystem Size Used\n/dev/disk1 500G 200G"}],
            text="Checking.", usage=92000, after="The disk is 40% full.")
    c.user("And the time?")
    c.tools([{"name": "Bash", "input": {"command": "date -u"}, "result": "Sun Nov  1 10:30:00 UTC 2026"}],
            usage=112000, after="It's 10:30 UTC.")
    return c


def chatty(sid="22222222-2222-4222-8222-000000000002"):
    c = Conv(sid)
    for i in range(12):
        c.user(f"Question {i}: tell me about tide pool creature number {i}.")
        if i % 4 == 2:
            c.tools([{"name": "Read", "input": {"file_path": f"/tmp/tides-fixture/notes/{i}.md"},
                      "result": f"notes on creature {i}\n" * 20}], usage=8000 * (i + 1))
        c.say(f"Creature {i} is a small animal that lives between the tides. " * 3, usage=8000 * (i + 1) + 500,
              thinking=f"thinking about creature {i}")
    return c


def work(c, k, tag, n_turns=3, usage_from=None):
    """A few more turns with tool calls, after a wake. tag makes every string unique."""
    u = usage_from or (c.usage + 1000)
    for i in range(n_turns):
        c.user(f"[{tag}] step {i}: run the check for {k}.{i}")
        c.tools([{"name": "Bash", "input": {"command": f"echo {tag}-{k}-{i} && ls /tmp/{tag}",
                                            "description": f"Check {tag} {i}"},
                  "result": f"{tag}-{k}-{i}\n" + "file.txt\n" * (5 + i)}],
                text=f"[{tag}] running check {i}.", usage=u + 6000 * i, after=f"[{tag}] check {i} passed.")
    return c
