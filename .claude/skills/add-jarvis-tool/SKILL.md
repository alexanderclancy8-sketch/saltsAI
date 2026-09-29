---
name: add-jarvis-tool
description: >
  Use this whenever Jarvis (the AI assistant built in this repository) needs a new capability it can call
  during conversation - a new thing the owner can ask it to do. Triggers on requests like "add a tool for X",
  "let Jarvis do X", "Jarvis needs to be able to X/Y/Z", "give it the ability to...", or any time a feature
  request in this repo turns out to need a new entry in jarvis/brain/tools.py's TOOLS list. Also use it when
  reviewing or extending an existing tool, since the same gating decision applies. Captures the exact recipe
  already used for log_job, log_purchase_order, run_security_review, self_improve, create_automation and
  others - input model, handler, the approval-gating decision (the part most likely to be got wrong), Tool
  registration, and the test pattern - so a new tool lands consistent with the rest of the codebase instead of
  reinventing (or skipping) the safety reasoning each time.
---

# Adding a tool to Jarvis

Every capability Jarvis can use in conversation is one `Tool(...)` entry in the `TOOLS` list in
`jarvis/brain/tools.py` - that file is the only place tools are defined; there's no other registry. Both
backends (`JarvisBrain` in `agent.py`, `MaxBrain` in `max_backend.py`) read from the same list, so a tool added
here works on either.

## 1. The input model

Add a pydantic `BaseModel` near other related input models in `tools.py`. Field descriptions matter - they
become the JSON schema Claude sees, so they're effectively the tool's documentation to the model itself. Give
concrete examples in the description where the format isn't obvious (a cron string, a date format, what "leave
blank" means):

```python
class CreateAutomationIn(BaseModel):
    description: str = Field(description="Short label for what this is, e.g. 'Weekday overdue-jobs check'")
    cron: str = Field(description="Standard 5-field crontab schedule in the company's local timezone, e.g. "
                                  "'0 8 * * 1-5' for 8am on weekdays, '*/30 * * * *' for every 30 minutes")
```

## 2. The handler

An `async def name(j, a: InputModel)` next to the input model. `j` is the whole `Jarvis` instance - reach into
whatever service already exists (`j.fsm`, `j.stores`, `j.actions`, `j.db`, `j.automations`, ...) rather than
duplicating logic that's already there. Most handlers are a few lines that delegate to a service method that
does the actual work - the handler's job is mainly translating the tool's input shape into that service's call.

## 3. The gating decision - the part to get right

This is the one step worth stopping and thinking about, because it's the actual safety mechanism, not
boilerplate. The rule underneath all of it: **nothing changes anything without the owner's approval.** There
are three valid shapes, pick based on what the tool actually does:

**a) `approval=True` on the `Tool` itself.** For anything that writes data directly and has nothing useful to
do beforehand - `dispatch()` (in `tools.py`) intercepts it and queues a `pending_actions` row *before the
handler ever runs at all*. Use this when the whole point of the call is the write:
```python
Tool("stock_move", "Record a stock movement...", StockMoveIn, stock_move, "Updating stock",
     approval=True, describe=lambda a: f"Stock: {a.kind} {a.qty:g} x {a.item}")
```
The `describe` lambda renders the summary shown on the approval card - always add one when the default
(`f"{label}: {args.model_dump_json()}"`) wouldn't read naturally to the owner.

**b) The handler does safe work itself, then calls `j.actions.queue(kind, summary, payload)` explicitly.** Use
this when *most* of the tool's work is safe (resolving an item name, pricing it, building an email body) and
only the final write needs a human's eyes on it - queuing after doing that work produces a much more useful
approval card than queuing the raw input would:
```python
async def log_purchase_order(j, a: LogPurchaseOrderIn):
    await j.stores.sync()
    lines = [...]  # resolve items and price them - safe, read-only
    action_id = j.actions.queue("email_send", f"Purchase order to {a.supplier} (£{total:,.2f} ex VAT)",
                                {"to": [a.supplier_email], ...})
    return {"queued_action": action_id, ...}
```

**c) Neither - for read-only tools, or background jobs that queue their own approval later.** A tool that only
returns information (`list_automations`, `stock_levels`) needs no gate at all. A tool that kicks off a
long-running background task (`run_security_review`, `self_improve`, `create_automation`) also needs none *at
the point of starting it*, because anything consequential that background job eventually wants to do goes
through its own approval gate when it gets there - `self_improve` opens a pull request but can never merge or
deploy it; `create_automation`'s scheduled runs go through the exact same tool-dispatch approval rules as a
live conversation turn.

If you're unsure which shape fits: does calling this tool, by itself, change something a human hasn't seen yet?
If yes, it needs (a) or (b). If it only looks something up, prepares something reversible, or schedules future
work that will itself ask for approval, it needs neither.

## 4. Register it

One `Tool(name, description, InputModel, handler, label, approval=..., describe=...)` line in the `TOOLS` list,
placed near conceptually related tools (not at the end) so the file stays organised. The `description` is what
Claude uses to decide *when* to call the tool - say what it's for and any constraint that matters (needs
approval, runs in the background, only handles X not Y), not just what it does mechanically.

## 5. Tests

Follow the pattern already used across `tests/test_*.py` (see `test_automations.py`, `test_fsm_and_purchasing_tools.py`
for recent examples):

```python
from jarvis.brain.tools import YourInputModel, TOOLS_BY_NAME
from jarvis.core import Jarvis
from tests.fakes import FakeClient, message, text_block, tool_block

def make(settings, script=None):
    return Jarvis(settings, client=FakeClient(script))
```

Two useful styles, use whichever fits:
- **Direct handler test** (fast, no LLM round-trip): `result = await your_handler(j, YourInputModel(...))`, then
  assert on the return value and, for gated tools, on `j.db.pending_actions()`.
- **Full-loop test** (proves the tool is actually reachable through conversation): script a `FakeClient` with a
  `message([tool_block("your_tool", {...})], "tool_use")` followed by a final text `message`, call
  `await j.brain.ask(...)`, then assert the same things.

For a tool gated with `approval=True`, also check the queued kind is `f"tool:{name}"` and that approving it
(`await j.actions.approve(pending[0]["id"])`) actually runs the handler.

## 6. Run the full suite before calling it done

```bash
python -m pytest -q
```

Not just the new test file - the whole suite. This has caught real, non-obvious bugs on tools built this exact
way in this repo: a `_client_key` cache tuple that didn't include the model, silently serving voice replies on
the wrong model; a `results[-1] = {...}` overwrite in an error handler that crashed with `IndexError` when the
*first* tool call in a batch failed rather than a later one; a Jarvis-construction ordering bug where a new
service was referenced by `connections()` (called during brain startup) before it had been assigned yet. None
of these were visible from the new test alone - they only showed up because something else in the suite already
exercised the code path differently. Fix whatever the full run turns up before considering the tool finished.
