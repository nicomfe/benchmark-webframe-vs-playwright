"""Scenario definitions and pass conditions (Spec section 6).

Each scenario is a "verification check" an agent might perform. Every
harness must reach the same verified outcome; the pass condition is judged
here, uniformly, from the harness result::

    CheckResult = {
        "stdout": [str],            # console lines as the tool reported them
        "errors": [{"type", "text"}],  # every error signal the tool exposed
        "eval_result": any | None,  # decoded JSON value of the eval expression
        "exit_code": int | None,    # process exit code for cold runs
        ...
    }

A scenario may define ``eval`` (a JS expression evaluated in the page after
``wait_for`` was seen), and ``expect(result) -> (passed: bool, reason: str,
extra: dict)``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

HELLO_LINE = "Hello, world!"
LI_COUNT_EVAL = "document.querySelectorAll('li').length"
SPA_ASSERT_EVAL = (
    "document.querySelectorAll('#list li').length === 25 && "
    "document.querySelector('#title').textContent === 'Items (25)' && "
    "document.querySelectorAll('#list li.done').length === 8"
)
LOOP_EVAL = "document.querySelectorAll('li').length + '|' + document.querySelector('h1').dataset.rev"


@dataclass
class Scenario:
    id: str
    label: str
    page: str
    wait_for: str | None
    eval: str | None
    expect: Callable[[dict], tuple[bool, str, dict]]
    # For cold runs: which exit codes count as "the tool finished normally".
    ok_exit_codes: tuple[int, ...] = (0,)
    description: str = ""
    extra: dict = field(default_factory=dict)


def _has_line(stdout: list[str], line: str) -> bool:
    return any(s == line for s in stdout)


def _exit_ok(res: dict, codes: tuple[int, ...]) -> tuple[bool, str]:
    code = res.get("exit_code")
    if code is None:  # warm configurations: no process exit per check
        return True, ""
    if code in codes:
        return True, ""
    return False, f"exit code {code}"


def expect_s1(res: dict):
    ok, why = _exit_ok(res, (0,))
    if not ok:
        return False, why, {}
    # Browsers warn that a script may not close a window it did not open.
    # That warning is not an error signal; filter it from the "exact output" check.
    lines = [s for s in res["stdout"] if not s.startswith("[warning]")]
    if lines == [HELLO_LINE]:
        return True, "", {}
    return False, f"stdout was {lines!r}", {}


def expect_s2(res: dict):
    ok, why = _exit_ok(res, (0,))
    if not ok:
        return False, why, {}
    if not _has_line(res["stdout"], "READY"):
        return False, "READY not seen", {}
    if res["errors"]:
        return False, f"unexpected errors: {res['errors'][:2]}", {}
    return True, "", {}


def _classify_message(errors: list[dict], needles: list[str]) -> str:
    """How informative was the captured error? full+stack > message > generic > none."""
    if not errors:
        return "none"
    text = "\n".join(e.get("text", "") for e in errors)
    has_needle = any(n in text for n in needles)
    has_location = bool(re.search(r":\d+(:\d+)?\b", text)) or "http://" in text
    if has_needle and has_location:
        return "full+stack" if re.search(r"(\n\s+at |@http|\n\s+\w*@)", text) or "http://" in text else "message+location"
    if has_needle:
        return "message"
    return "generic"


def make_expect_s3(needles: list[str]):
    def expect(res: dict):
        detected = bool(res["errors"]) or (res.get("exit_code") not in (None, 0))
        quality = _classify_message(res["errors"], needles)
        extra = {"detected": detected, "message_quality": quality, "error_count": len(res["errors"])}
        if not detected:
            return False, "failure not detected", extra
        return True, "", extra

    return expect


def expect_s4(res: dict):
    ok, why = _exit_ok(res, (0,))
    if not ok:
        return False, why, {}
    if res.get("eval_result") == 100:
        return True, "", {}
    return False, f"eval returned {res.get('eval_result')!r}", {}


def expect_s5(res: dict):
    ok, why = _exit_ok(res, (0,))
    if not ok:
        return False, why, {}
    if not _has_line(res["stdout"], "READY"):
        return False, "READY not seen", {}
    if res.get("eval_result") is True:
        return True, "", {}
    return False, f"assertion returned {res.get('eval_result')!r}", {}


def expect_s6(res: dict):
    ok, why = _exit_ok(res, (0,))
    if not ok:
        return False, why, {}
    for s in res["stdout"]:
        if s.startswith("TIME "):
            try:
                ms = float(s[5:])
            except ValueError:
                return False, f"bad TIME line {s!r}", {}
            engine = next((e[7:] for e in res["stdout"] if e.startswith("ENGINE ")), "")
            return True, "", {"js_ms": ms, "engine_ua": engine}
    return False, "TIME line not seen", {}


def make_expect_loop(rev: int):
    want = f"100|{rev}"

    def expect(res: dict):
        ok, why = _exit_ok(res, (0,))
        if not ok:
            return False, why, {}
        if not _has_line(res["stdout"], "READY"):
            return False, "READY not seen", {}
        if res["errors"]:
            return False, f"unexpected errors: {res['errors'][:2]}", {}
        if res.get("eval_result") == want:
            return True, "", {}
        return False, f"eval returned {res.get('eval_result')!r}, want {want!r}", {}

    return expect


SCENARIOS: list[Scenario] = [
    Scenario(
        "S1", "hello: load, capture stdout, exit", "hello.html", HELLO_LINE, None, expect_s1,
        description="Output equals the expected line; cold processes exit 0.",
    ),
    Scenario(
        "S2", "ok: wait for READY, no errors", "ok.html", "READY", None, expect_s2,
        description="READY seen and no exception / error signal.",
    ),
    Scenario(
        "S3-throw", "broken-throw: detect uncaught exception", "broken-throw.html", "READY", None,
        make_expect_s3(["boom: intentional uncaught exception"]), ok_exit_codes=(0, 1),
        description="Failure detected; message informativeness recorded.",
    ),
    Scenario(
        "S3-rejection", "broken-rejection: detect unhandled rejection", "broken-rejection.html", "READY", None,
        make_expect_s3(["nope: intentional unhandled rejection"]), ok_exit_codes=(0, 1),
    ),
    Scenario(
        "S3-resource", "broken-resource: detect missing img/script", "broken-resource.html", "READY", None,
        make_expect_s3(["missing.png", "missing.js"]), ok_exit_codes=(0, 1),
    ),
    Scenario(
        "S4", "ok: evaluate li count", "ok.html", "READY", LI_COUNT_EVAL, expect_s4,
        description="Eval returns 100.",
    ),
    Scenario(
        "S5", "spa: wait READY, DOM assertion", "spa.html", "READY", SPA_ASSERT_EVAL, expect_s5,
        description="Assertion true after the SPA fetched and rendered JSON.",
    ),
    Scenario(
        "S6", "heavy: capture reported JS time", "heavy.html", "TIME ", None, expect_s6,
        description="TIME line captured; the in-page ms is recorded as js_ms.",
    ),
]

LOOP_SCENARIO_ID = "S7"
LOOP_PAGE = "_scratch/ok.html"  # written by the runner for each iteration

BY_ID = {s.id: s for s in SCENARIOS}


def ok_html_with_rev(template: str, rev: int) -> str:
    return template.replace('data-rev="0">Items rev 0<', f'data-rev="{rev}">Items rev {rev}<')
