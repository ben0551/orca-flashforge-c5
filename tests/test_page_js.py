"""Run the device tab's JavaScript outside OrcaSlicer.

Extracts the page from the plugin, feeds it the plugin's own normalised /detail
payloads, and runs tests/page_harness.js under node.

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
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import fake_orca                                      # noqa: E402
from mock_printer import MockServer                    # noqa: E402


def page_ids(page):
    """Every element id the markup actually defines."""
    return sorted(set(re.findall(r'id="([A-Za-z0-9_-]+)"', page)))


def missing_elements(page, js):
    """Ids the script looks up that the markup never defines.

    A null element write throws and aborts the whole inline script, so this is
    the failure the DOM stub exists to catch — and it has to come from the
    markup, or a stale stub would hide exactly the bug it guards.
    """
    looked_up = set(re.findall(r'\$\("#?([A-Za-z0-9_-]+)"\)', js))
    looked_up |= set(re.findall(r'getElementById\("([A-Za-z0-9_-]+)"\)', js))
    return sorted(looked_up - set(page_ids(page)))


def extract_page(source_path):
    text = open(source_path, encoding="utf-8").read()
    match = re.search(r'^PAGE = r"""(.*?)^"""$', text, re.S | re.M)
    if not match:
        raise SystemExit("could not find the PAGE block in the plugin")
    page = match.group(1)
    start = page.index("<script>") + len("<script>")
    end = page.rindex("</script>")
    return page, page[start:end]


def main():
    if shutil.which("node") is None:
        print("node is not installed — skipping the page checks")
        return 0

    source = os.path.join(ROOT, "flashforge-c5_any.py")
    page, js = extract_page(source)

    work = tempfile.mkdtemp(prefix="ffc5-page-")
    try:
        js_path = os.path.join(work, "page.js")
        with open(js_path, "w", encoding="utf-8") as fh:
            fh.write(js)
        ids_path = os.path.join(work, "page-ids.json")
        with open(ids_path, "w", encoding="utf-8") as fh:
            json.dump(page_ids(page), fh)

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
            now = int(time.time())
            idle = dict(idle, autoShutdown="close", autoShutdownTime=0)
            json.dump({"idle": idle, "live": live,
                       "temps": [{"t": now - 10, "n": 200.0, "b": 60.0},
                                 {"t": now - 5, "n": 205.0, "b": 61.0},
                                 {"t": now, "n": 210.0, "b": 62.0}]}, fh, indent=1)

        print("Page markup checks")
        missing = missing_elements(page, js)
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
            ("the tab offers a live camera view",
             'id="bcam"' in page and 'id="camimg"' in page),
            ("the maintenance card exists and says there is nothing to jog",
             'id="blevel"' in page and 'id="bshaper"' in page
             and 'id="btimer"' in page and "nothing to jog" in page),
            ("the printer picker and the fleet controls exist",
             'id="printer"' in page and 'id="badd"' in page
             and 'id="bremove"' in page and 'id="bfleet"' in page
             and 'id="attention"' in page and 'id="fleet"' in page),
            ("the fleet starts hidden until it is opened",
             'id="fleet" class="hidden"' in page),
            ("the temperature chart and the history card exist",
             'id="chart"' in page and 'id="hist"' in page
             and 'id="hjobs"' in page and 'id="hrate"' in page),
            ("the chart follows the theme rather than hardcoding colours",
             'getPropertyValue("--accent")' in js),
            ("every element the script looks up is defined in the markup",
             not missing, str(missing)),
            # OrcaSlicer injects these into every plugin page and puts
            # data-orca-theme on <html>. The tab must follow the app, not pin a theme.
            ("the page takes its surfaces from OrcaSlicer",
             all(v in page for v in ("--orca-bg", "--orca-fg",
                                     "--orca-border", "--orca-muted"))),
            ("and no surface is hardcoded dark",
             not re.search(r"--(bg|panel|panel2|line)\s*:\s*#[0-9a-fA-F]{3,6}", page)),
            ("the primary button uses the accent's own foreground",
             "var(--orca-accent-fg" in page),
            # A stray character before :root makes the whole palette block dead CSS:
            # every var() then resolves to nothing, so buttons lose their background
            # and border while keeping the host's inherited text colour.
            ("the palette block is a real :root rule",
             re.search(r"(?m)^[ \t]*:root\{", page) is not None
             and not re.search(r"(?m)^[ \t]*\S[^\n]*:root\{", page)),
            ("the page's buttons outrank the host's injected defaults",
             "body button" in page
             and re.search(r"(?<![\\w.#:>~ ])button\\s*\\{", page) is None),
        ]
        failed = 0
        for name, ok, *rest in checks:
            detail = rest[0] if rest and not ok else ""
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
                  + (f" — {detail}" if detail else ""))
            failed += 0 if ok else 1

        result = subprocess.run(["node", os.path.join(HERE, "page_harness.js"),
                                 js_path, states_path, ids_path],
                                capture_output=True, text=True)
        print(result.stdout)
        if result.stderr.strip():
            print(result.stderr.strip())
        failed += 0 if result.returncode == 0 else 1
        return 1 if failed else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
