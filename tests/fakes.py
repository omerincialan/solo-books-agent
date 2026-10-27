"""Test doubles: an in-memory Google Sheet and a scripted Muse Spark."""
import json
import re
import gspread


class FakeWorksheet:
    def __init__(self, name):
        self.name, self.rows = name, []

    def append_row(self, row, value_input_option=None):
        self.rows.append(["" if v is None else str(v) for v in row])

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def update(self, values, range_name, value_input_option=None):
        n = int(re.search(r"(\d+)", range_name).group(1))
        self.rows[n - 1] = ["" if v is None else str(v) for v in values[0]]

    def delete_rows(self, n):
        del self.rows[n - 1]


class FakeSheet:
    def __init__(self):
        self.tabs = {}

    def worksheet(self, name):
        if name not in self.tabs:
            raise gspread.WorksheetNotFound(name)
        return self.tabs[name]

    def add_worksheet(self, name, rows, cols):
        self.tabs[name] = FakeWorksheet(name)
        return self.tabs[name]


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeToolCall:
    def __init__(self, i, name, args):
        self.id, self.type = f"call_{i}", "function"
        self.function = _Obj(name=name, arguments=json.dumps(args))

    def model_dump(self):
        return {"id": self.id, "type": "function",
                "function": {"name": self.function.name, "arguments": self.function.arguments}}


class ScriptedModel:
    """Each step is either a list of (tool, args) or a final text (callable gets the tool results)."""

    def __init__(self, steps):
        self.steps, self.requests, self._n = list(steps), [], 0
        self.chat = _Obj(completions=_Obj(create=self.create))

    def create(self, model, messages, tools=None, **kw):
        self.requests.append({"model": model, "messages": json.loads(json.dumps(messages)), "tools": tools})
        step = self.steps.pop(0)
        if isinstance(step, list):
            calls = []
            for name, args in step:
                self._n += 1
                calls.append(FakeToolCall(self._n, name, args))
            message = _Obj(content="", tool_calls=calls)
        else:
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            message = _Obj(content=step(results) if callable(step) else step, tool_calls=None)
        return _Obj(choices=[_Obj(message=message)])
