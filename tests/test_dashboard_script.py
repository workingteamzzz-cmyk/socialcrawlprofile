"""The dashboard's script parses: one syntax error (e.g. a name declared twice) stops every button on every page."""
import re
import shutil
import subprocess

import pytest

STATIC = __import__("os").path.join(__import__("os").path.dirname(__file__), "..", "crawler", "static")


@pytest.mark.parametrize("page", ["index.html", "client-docs.html"])
def test_page_scripts_parse(tmp_path, page):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    with open(__import__("os").path.join(STATIC, page), encoding="utf-8") as f:
        html = f.read()
    for i, code in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
        js = tmp_path / f"{page}.{i}.js"
        js.write_text(code, encoding="utf-8")
        r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:500]


def test_no_top_level_name_declared_twice():
    """Without node too: a const / let / function name declared twice at the top level of the main script."""
    with open(__import__("os").path.join(STATIC, "index.html"), encoding="utf-8") as f:
        code = re.findall(r"<script>(.*?)</script>", f.read(), re.S)[-1]
    names = re.findall(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=|^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
                       code, re.M)
    seen, dup = set(), set()
    for a, b in names:
        n = a or b
        (dup if n in seen else seen).add(n)
    assert not dup, dup
