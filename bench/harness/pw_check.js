// Shared Playwright "verification check" logic used by pw_cold.js and
// pw_warm.js. One check = open a page, load a URL, collect console output
// and every error signal Playwright exposes, wait for a marker line, run an
// optional JS expression, close the page.
//
// Request shape (JSON):
//   { id, url, waitFor: "READY", eval: "<js expression>" | null, timeoutMs }
// Response shape (JSON):
//   { id, ok, stdout: [lines], errors: [{type, text}], evalResult: <json>,
//     evalError: string|null, t: { newPageMs, gotoMs, waitMs, evalMs, closeMs } }
//
// Error signals captured (all are things an agent would wire up):
//   pageerror      uncaught exceptions and unhandled rejections
//   console.error  console messages at level "error" (Chromium reports failed
//                  resource loads this way)
//   requestfailed  network-level failures (DNS, connection refused, abort)
//   http>=400      responses with an error status (404 for a missing <img>)
"use strict";

const { performance } = require("node:perf_hooks");

function now() {
  return performance.now();
}

async function runCheck(browser, req) {
  const t = {};
  const stdout = [];
  const errors = [];
  const timeoutMs = req.timeoutMs || 15000;
  const waitFor = req.waitFor || null;

  let markerSeen;
  const marker = new Promise((resolve) => (markerSeen = resolve));
  if (!waitFor) markerSeen(true);

  let t0 = now();
  const page = await browser.newPage();
  t.newPageMs = now() - t0;

  page.on("console", (msg) => {
    const type = msg.type();
    const text = msg.text();
    stdout.push(type === "log" ? text : `[${type}] ${text}`);
    if (type === "error") errors.push({ type: "console.error", text });
    if (waitFor && (text === waitFor || text.startsWith(waitFor))) markerSeen(true);
  });
  page.on("pageerror", (err) => {
    errors.push({ type: "pageerror", text: err.stack || String(err) });
  });
  page.on("requestfailed", (r) => {
    errors.push({ type: "requestfailed", text: `${r.url()} ${(r.failure() || {}).errorText || ""}`.trim() });
  });
  page.on("response", (r) => {
    if (r.status() >= 400) errors.push({ type: "http", text: `${r.status()} ${r.url()}` });
  });

  let ok = true;
  let evalResult = null;
  let evalError = null;
  let timedOut = false;

  let timerHandle;
  const timer = new Promise((resolve) => {
    timerHandle = setTimeout(() => resolve("timeout"), timeoutMs);
    // Do not keep the node event loop alive just for this timer.
    if (timerHandle.unref) timerHandle.unref();
  });
  try {
    t0 = now();
    // waitUntil "load" is Playwright's default; we keep it. The marker wait
    // below is what actually gates "the page says it is ready".
    await Promise.race([page.goto(req.url, { waitUntil: "load" }), timer]);
    t.gotoMs = now() - t0;

    t0 = now();
    const r = await Promise.race([marker, timer]);
    if (r === "timeout") timedOut = true;
    t.waitMs = now() - t0;

    if (req.eval && !timedOut) {
      t0 = now();
      try {
        evalResult = await Promise.race([page.evaluate(req.eval), timer]);
        if (evalResult === "timeout") timedOut = true;
      } catch (e) {
        evalError = String(e && e.message ? e.message : e);
      }
      t.evalMs = now() - t0;
    }
  } catch (e) {
    ok = false;
    errors.push({ type: "harness", text: String(e && e.message ? e.message : e) });
  }

  clearTimeout(timerHandle);
  t0 = now();
  try {
    await page.close();
  } catch (_) {
    /* page may already be gone */
  }
  t.closeMs = now() - t0;

  if (timedOut) {
    ok = false;
    errors.push({ type: "harness", text: `timeout after ${timeoutMs}ms waiting for ${waitFor || "eval"}` });
  }

  return { id: req.id, ok, stdout, errors, evalResult, evalError, t };
}

function launchOptions(engine) {
  // Sensible headless defaults, nothing tuned. Playwright already passes
  // --no-sandbox to Chromium unless chromiumSandbox:true, which matters in
  // containers. Headless Chromium here is Playwright's "chromium headless
  // shell" build (the 1.49+ default for headless:true).
  return { headless: true };
}

function getEngine(name) {
  const pw = require("playwright");
  if (!pw[name]) throw new Error(`unknown engine ${name}`);
  return pw[name];
}

module.exports = { runCheck, launchOptions, getEngine, now };
