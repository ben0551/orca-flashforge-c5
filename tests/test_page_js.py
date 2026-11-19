"""Run each tab's JavaScript outside OrcaSlicer.

Extracts a page from the plugin that ships it, feeds it that plugin's own
normalised payloads, and runs the matching node harness. The device tab comes
from flashforge-c5_any.py, the MakerWorld tab from makerworld-c5_any.py.

    python3 tests/test_page_js.py
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import fake_orca                                      # noqa: E402
from mock_printer import MockServer                    # noqa: E402


def extract_page(source_path):
    text = open(source_path, encoding="utf-8").read()
    match = re.search(r'^PAGE = r"""(.*?)^"""$', text, re.S | re.M)
    if not match:
        raise SystemExit("could not find the PAGE block in the plugin")
    page = match.group(1)
    start = page.index("<script>") + len("<script>")
    end = page.rindex("</script>")
    return page, page[start:end]


def extract_makerworld_page(source_path):
    """The second tab, which is a plain (non-raw) string."""
    text = open(source_path, encoding="utf-8").read()
    match = re.search(r'^MAKERWORLD_PAGE = """(.*?)^"""$', text, re.S | re.M)
    if not match:
        raise SystemExit("could not find the MAKERWORLD_PAGE block in the plugin")
    page = match.group(1)
    start = page.index("<script>") + len("<script>")
    end = page.rindex("</script>")
    return page, page[start:end]


def run_makerworld_checks(source, work):
    """Static checks plus the node harness for the MakerWorld tab."""
    page, js = extract_makerworld_page(source)
    js_path = os.path.join(work, "makerworld.js")
    with open(js_path, "w", encoding="utf-8") as fh:
        fh.write(js)

    print("MakerWorld tab markup")
    checks = [
        ("the page has exactly one script block",
         page.count("<script>") == 1 and page.count("</script>") == 1),
        ("the $-helper tolerates both 'id' and '#id' spellings",
         'charAt(0) === "#"' in js),
        ("every showBanner call sets display, not just text",
         'banner.style.display = text ? "block" : "none"' in js),
        ("no nested template literals (a parse error in the webview)",
         not re.search(r"\$\{[^}]*`", js)),
        ("no optional-chaining method calls inside a template literal",
         not re.search(r"\$\{[^}]*\?\.\(", js)),
        ("the token itself is never sent back to the page",
         "token_set" in js and not re.search(r"message\.token\b(?!_set)", js)),
        ("the fallback names a menu item that exists",
         "Open Project" in js),
        ("the page asks for its state on load rather than waiting for a timer",
         'post({ type: "hello" })' in js),
    ]
    failed = 0
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        failed += 0 if ok else 1

    result = subprocess.run(["node", os.path.join(HERE, "makerworld_harness.js"), js_path],
                            capture_output=True, text=True)
    print(result.stdout)
    if result.stderr.strip():
        print(result.stderr.strip())
    failed += 0 if result.returncode == 0 else 1
    return failed


def main():
    if shutil.which("node") is None:
        print("node is not installed — skipping the page checks")
        return 0

    device_source = os.path.join(ROOT, "flashforge-c5_any.py")
    makerworld_source = os.path.join(ROOT, "makerworld-c5_any.py")
    source = device_source
    page, js = extract_page(source)

    work = tempfile.mkdtemp(prefix="ffc5-page-")
    try:
        js_path = os.path.join(work, "page.js")
        with open(js_path, "w", encoding="utf-8") as fh:
            fh.write(js)

        # Real payloads from the plugin, not hand-written fixtures.
        sys.modules["orca"] = fake_orca.make_module()
        spec = importlib.util.spec_from_file_location("ffc5_page_under_test", source)
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)

        with MockServer() as server:
            server.printer.set(lightStatus="close")
            idle = plugin.normalize_detail(server.printer.detail())
        with MockServer() as server:
            server.printer.set(status="printing", printProgress=0.415,
                               printDuration=600, estimatedTime=900, printLayer=88,
                               targetPrintLayer=200, printFileName="widget.gcode",
                               lightStatus="open", platTemp=59.5,
                               platTargetTemp=60.0, chamberTemp=31.0)
            live = plugin.normalize_detail(server.printer.detail())

        states_path = os.path.join(work, "states.json")
        with open(states_path, "w", encoding="utf-8") as fh:
            json.dump({"idle": idle, "live": live}, fh, indent=1)

        print("Page markup checks")
        checks = [
            ("the page has exactly one script block",
             page.count("<script>") == 1 and page.count("</script>") == 1),
            ("the $-helper tolerates both 'id' and '#id' spellings",
             'replace(/^#/, "")' in js),
            ("every showBanner call sets display, not just text",
             'b.style.display = "block"' in js),
            ("no nested template literals (a parse error in the webview)",
             not re.search(r"\$\{[^}]*`", js)),
            ("no optional-chaining method calls inside a template literal",
             not re.search(r"\$\{[^}]*\?\.\(", js)),
            ("the stored-file rows offer a preview",
             'data-preview="' in js),
            ("the connection panel offers to load OrcaSlicer's own settings",
             'id="borca"' in page and 'type: "load_orca"' in js),
            ("the spool card shows a colour name, not a hex code",
             's.color_name' in js),
        ]
        failed = 0
        for name, ok in checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            failed += 0 if ok else 1

        result = subprocess.run(["node", os.path.join(HERE, "page_harness.js"),
                                 js_path, states_path],
                                capture_output=True, text=True)
        print(result.stdout)
        if result.stderr.strip():
            print(result.stderr.strip())
        failed += 0 if result.returncode == 0 else 1

        print()
        failed += run_makerworld_checks(makerworld_source, work)
        return 1 if failed else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
