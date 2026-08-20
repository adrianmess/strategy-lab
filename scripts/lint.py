#!/usr/bin/env python3
"""Repo lint: catch the classes of bug that have actually broken this lab.

  python3 scripts/lint.py            # everything, summary + exit code
  python3 scripts/lint.py --fatal    # only the things that break the app
  python3 scripts/lint.py --files panel/server.py panel/panel.html

Checks
  PY-SYNTAX   every .py compiles                                  (fatal)
  PY-NAMES    pyflakes undefined names / redefinitions            (fatal)
  PY-UNUSED   pyflakes unused imports and locals                  (warn)
  JS-SYNTAX   the JS inside every .html parses via `node --check` (fatal)
              — concatenated per file, exactly how the browser sees it, so a
              redeclared top-level `const` is caught. That one took the whole
              panel down once.
  JS-IDS      getElementById('x') where no id="x" exists in the file (warn)
  JSON-VALID  operational JSON parses (configs, instances, pools)  (fatal)
  ROUTES      no two Flask routes share a path or endpoint name    (fatal)
  INSTNAME    user-facing strings must not print a bare instance id (warn)
  TEMPLATE    config.json may only be opened through _template_cfg (warn)

Exit code is 1 if anything fatal fired.
"""
import argparse
import ast
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))

# JSON that the app actually reads at runtime. optimizer/runs holds ~27k
# generated files — walking them is minutes of IO for no signal, and a broken
# one fails loudly at its own use site anyway.
JSON_GLOBS = [
    "adaptive_trader/config*.json", "adaptive_trader/proxy_pool.json",
    "adaptive_trader/mexc_api_keys.json", "panel/instances.json",
    "optimizer/gamut_limits.json", "dashboard/bt_meta.json",
]
SKIP_DIRS = {".git", "__pycache__", "node_modules", "runs", "campaigns",
             "data", "venv", ".venv"}

findings = []          # (level, check, path, line, message)


def add(level, check, path, line, msg):
    findings.append((level, check, os.path.relpath(path, REPO), line, msg))


def _tracked(ext):
    """git-tracked files only: the tree also holds vendored samples and
    scratch copies that nobody ships, and their noise buries real findings."""
    try:
        r = subprocess.run(["git", "ls-files", f"*{ext}"], cwd=REPO,
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return [os.path.join(REPO, f) for f in r.stdout.split()
                    if not any(s + os.sep in f for s in SKIP_DIRS)]
    except FileNotFoundError:
        pass
    out = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        out += [os.path.join(root, f) for f in files if f.endswith(ext)]
    return out


def py_files():
    return _tracked(".py")


def html_files():
    return _tracked(".html")


# ---------------------------------------------------------------- python
def check_python(paths):
    for p in paths:
        src = open(p, encoding="utf-8", errors="replace").read()
        try:
            ast.parse(src, filename=p)
        except SyntaxError as e:
            add("FATAL", "PY-SYNTAX", p, e.lineno or 0, e.msg)
            continue
    if not paths:
        return
    # pyflakes may be installed for a different interpreter than the one
    # running this script, so try the CLI first. A linter that silently
    # reports nothing because its own tool is missing is worse than no linter:
    # say so loudly.
    cmd = None
    for candidate in (["pyflakes"], [sys.executable, "-m", "pyflakes"]):
        try:
            probe = subprocess.run(candidate + ["--version"],
                                   capture_output=True, text=True)
            if probe.returncode == 0:
                cmd = candidate
                break
        except FileNotFoundError:
            continue
    if cmd is None:
        add("FATAL", "PY-NAMES", os.path.join(REPO, "scripts/lint.py"), 0,
            "pyflakes is not installed — `pip3 install pyflakes "
            "--break-system-packages`; name checks did NOT run")
        return
    r = subprocess.run(cmd + paths, capture_output=True, text=True, cwd=REPO)
    for ln in (r.stdout + r.stderr).splitlines():
        m = re.match(r"^(.*?):(\d+):(?:\d+:)?\s*(.*)$", ln)
        if not m:
            if ln.strip() and "No module named" in ln:
                add("FATAL", "PY-NAMES", os.path.join(REPO, "scripts/lint.py"),
                    0, f"pyflakes failed to run: {ln.strip()}")
            continue
        path, line, msg = m.group(1), int(m.group(2)), m.group(3)
        # An undefined name always breaks at runtime. A shadowed import is
        # usually deliberate here (engine3.VARIANTS is re-imported on purpose
        # after it may have been rebuilt), so it is a warning, not a failure.
        fatal = "undefined name" in msg or "syntax" in msg.lower()
        add("FATAL" if fatal else "WARN",
            "PY-NAMES" if fatal else "PY-UNUSED", path, line, msg)


# ------------------------------------------------------------------- html
SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.S | re.I)


def check_html(paths):
    have_node = subprocess.run(["node", "--version"], capture_output=True).returncode == 0
    for p in paths:
        src = open(p, encoding="utf-8", errors="replace").read()
        blocks = SCRIPT_RE.findall(src)
        if blocks and have_node:
            # concatenated: the browser shares one top-level scope across the
            # inline <script> blocks of a page, so a duplicated `const` is a
            # real error even when each block parses alone
            tmp = os.path.join("/tmp", "_lint_" + os.path.basename(p) + ".js")
            open(tmp, "w").write("\n".join(blocks))
            r = subprocess.run(["node", "--check", tmp],
                               capture_output=True, text=True)
            if r.returncode != 0:
                first = [l for l in r.stderr.splitlines() if l.strip()]
                msg = next((l for l in first if "Error" in l), first[0] if first else "parse error")
                m = re.search(r":(\d+)$", first[0]) if first else None
                add("FATAL", "JS-SYNTAX", p, int(m.group(1)) if m else 0,
                    msg.strip())
            os.unlink(tmp)
        # getElementById('x') / $('x') against the ids the file declares.
        # Template-built ids (`tdot_${i}`) are skipped — they are dynamic.
        declared = set(re.findall(r"""\bid\s*=\s*["']([A-Za-z0-9_\-]+)["']""", src))
        declared |= set(re.findall(r"""\bid\s*=\s*["']([A-Za-z0-9_\-]*)\$\{""", src))
        for m in re.finditer(r"""getElementById\(\s*['"]([A-Za-z0-9_\-]+)['"]\s*\)""", src):
            name = m.group(1)
            if name not in declared:
                add("WARN", "JS-IDS", p, src[:m.start()].count("\n") + 1,
                    f"getElementById('{name}') but no id=\"{name}\" in this file")


# ------------------------------------------------------------------- json
def check_json():
    import glob
    for pat in JSON_GLOBS:
        for p in glob.glob(os.path.join(REPO, pat)):
            if any(s in p for s in (".bak.", ".deleted_", ".tmp")):
                continue
            try:
                json.load(open(p))
            except Exception as e:
                add("FATAL", "JSON-VALID", p, 0, str(e)[:120])


# ------------------------------------------------- flask routes / repo rules
def check_routes(paths):
    for p in paths:
        src = open(p, encoding="utf-8", errors="replace").read()
        if "@app.route" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        seen_path, seen_name = {}, {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            routed = False
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call)
                        and getattr(dec.func, "attr", "") == "route"):
                    continue
                if not dec.args or not isinstance(dec.args[0], ast.Constant):
                    continue
                rule = dec.args[0].value
                methods = "GET"
                for kw in dec.keywords:
                    if kw.arg == "methods":
                        try:
                            methods = ",".join(sorted(ast.literal_eval(kw.value)))
                        except Exception:
                            pass
                key = (rule, methods)
                if key in seen_path:
                    add("FATAL", "ROUTES", p, node.lineno,
                        f"route {rule} [{methods}] already defined at line "
                        f"{seen_path[key]} — the later one never runs")
                seen_path[key] = node.lineno
                routed = True
            # once per FUNCTION, not per decorator: stacking two @app.route
            # lines on one view is a legitimate alias, not a redefinition
            if routed:
                if node.name in seen_name:
                    add("FATAL", "ROUTES", p, node.lineno,
                        f"endpoint function '{node.name}' redefined "
                        f"(first at line {seen_name[node.name]}) — Flask "
                        f"raises on duplicate endpoint names")
                seen_name[node.name] = node.lineno


def check_repo_rules(paths):
    for p in paths:
        src = open(p, encoding="utf-8", errors="replace").read()
        rel = os.path.relpath(p, REPO)
        for i, line in enumerate(src.splitlines(), 1):
            # a message the user reads should name the instance, not number it
            if re.search(r"""jsonify\(error=.*instance \{[ij]\}""", line):
                add("WARN", "INSTNAME", p, i,
                    "user-facing error prints a bare instance id — use _iname(i)")
            # config.json is the adopt TEMPLATE; opening it bare is how adopt
            # broke when the file was deleted
            if rel == "panel/server.py" and 'AT, "config.json"' in line \
                    and "_template_cfg" not in src[max(0, src.find(line) - 400):src.find(line)]:
                if "os.path.join(AT, \"config.json\")" in line and "_template_cfg" not in line:
                    add("WARN", "TEMPLATE", p, i,
                        "opens config.json directly — go through _template_cfg(mode)")


# ------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fatal", action="store_true", help="hide warnings")
    ap.add_argument("--files", nargs="*", help="limit to these paths")
    a = ap.parse_args()

    if a.files:
        sel = [os.path.abspath(f) for f in a.files]
        pys = [f for f in sel if f.endswith(".py")]
        htmls = [f for f in sel if f.endswith(".html")]
    else:
        pys = sorted(py_files())
        htmls = sorted(html_files())

    check_python(pys)
    check_html(htmls)
    check_routes(pys)
    check_repo_rules(pys)
    if not a.files:
        check_json()

    fatal = [f for f in findings if f[0] == "FATAL"]
    warn = [f for f in findings if f[0] == "WARN"]
    shown = fatal if a.fatal else fatal + warn
    by_check = {}
    for lvl, chk, path, line, msg in shown:
        by_check.setdefault(chk, []).append((lvl, path, line, msg))
    for chk in sorted(by_check):
        rows = by_check[chk]
        print(f"\n=== {chk} ({len(rows)}) ===")
        for lvl, path, line, msg in sorted(rows)[:40]:
            print(f"  {lvl:5s} {path}:{line}: {msg}")
        if len(rows) > 40:
            print(f"  … {len(rows)-40} more")

    print(f"\nchecked {len(pys)} python files, {len(htmls)} html files")
    print(f"FATAL {len(fatal)} · WARN {len(warn)}")
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
