"""Stand-ins for the Colab/Jupyter pieces the team's script uses (ipywidgets form, display, file download), so the
script runs unchanged on the server. Values are set by runner.py; progress changes are reported through HOOK."""
from __future__ import annotations

from types import SimpleNamespace

HOOK = {"fn": None}          # fn(widget) is called when a progress bar or label changes


class _Widget:
    default = None

    def __init__(self, *args, **kw):
        object.__setattr__(self, "description", kw.get("description", ""))
        object.__setattr__(self, "options", kw.get("options"))
        value = kw.get("value", self.default)
        if value is None and kw.get("options"):
            first = list(kw["options"])[0]
            value = first[1] if isinstance(first, tuple) else first
        if value is None and args and isinstance(args[0], str):
            value = args[0]                                   # Label("text")
        object.__setattr__(self, "value", value)
        for k in ("min", "max", "bar_style"):
            if k in kw:
                object.__setattr__(self, k, kw[k])

    def __setattr__(self, name, val):
        object.__setattr__(self, name, val)
        if name in ("value", "max", "bar_style") and HOOK["fn"]:
            try:
                HOOK["fn"](self)
            except Exception:  # noqa: BLE001
                pass

    def observe(self, *a, **k):
        pass

    def on_click(self, *a, **k):
        pass


class Checkbox(_Widget):
    default = False


class IntText(_Widget):
    default = 0


class Text(_Widget):
    default = ""


class Textarea(Text):
    pass


class Password(Text):
    pass


class Dropdown(_Widget):
    pass


class Label(_Widget):
    default = ""


class HTML(Label):
    pass


class Button(_Widget):
    pass


class IntProgress(_Widget):
    default = 0

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if not hasattr(self, "max"):
            object.__setattr__(self, "max", 100)


class _Box(_Widget):
    def __init__(self, children=None, **kw):
        super().__init__(**kw)
        object.__setattr__(self, "children", children or [])


class Output(_Widget):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def clear_output(self, *a, **k):
        pass


widgets = SimpleNamespace(Checkbox=Checkbox, IntText=IntText, Text=Text, Textarea=Textarea, Password=Password,
                          Dropdown=Dropdown, Label=Label, HTML=HTML, Button=Button, IntProgress=IntProgress,
                          HBox=_Box, VBox=_Box, GridBox=_Box, Output=Output, Layout=lambda *a, **k: None)


def display(*a, **k):
    pass


files = SimpleNamespace(download=lambda *a, **k: None, upload=lambda *a, **k: {})
