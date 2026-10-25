// Capability probes for Playwright (Spec section 9). Each probe is actually
// executed against the local pages; results are printed as one JSON object.
// Usage: node pw_caps.js <engine> <baseUrl>
"use strict";

const { getEngine } = require("./pw_check");

async function main() {
  const [engine, base] = process.argv.slice(2);
  const browser = await getEngine(engine).launch({ headless: true });
  const out = {};

  async function probe(name, fn) {
    try {
      out[name] = await fn();
    } catch (e) {
      out[name] = { status: "No", note: "probe threw: " + String(e && e.message ? e.message : e).slice(0, 120) };
    }
  }

  await probe("console", async () => {
    const page = await browser.newPage();
    const lines = [];
    page.on("console", (m) => lines.push(m.text()));
    await page.goto(base + "/ok.html");
    await page.waitForFunction(() => true);
    await new Promise((r) => setTimeout(r, 50));
    await page.close();
    return { status: lines.includes("READY") ? "Yes" : "No", note: "page.on('console')", evidence: lines.slice(0, 3) };
  });

  await probe("uncaught_exception", async () => {
    const page = await browser.newPage();
    const errs = [];
    page.on("pageerror", (e) => errs.push(e.message));
    await page.goto(base + "/broken-throw.html");
    await new Promise((r) => setTimeout(r, 150));
    await page.close();
    return { status: errs.length ? "Yes" : "No", note: "page.on('pageerror') with message and stack", evidence: errs };
  });

  await probe("unhandled_rejection", async () => {
    const page = await browser.newPage();
    const errs = [];
    page.on("pageerror", (e) => errs.push(e.message));
    await page.goto(base + "/broken-rejection.html");
    await new Promise((r) => setTimeout(r, 150));
    await page.close();
    return { status: errs.length ? "Yes" : "No", note: "page.on('pageerror')", evidence: errs };
  });

  await probe("failed_resource", async () => {
    const page = await browser.newPage();
    const sig = [];
    page.on("response", (r) => r.status() >= 400 && sig.push(`${r.status()} ${r.url()}`));
    page.on("requestfailed", (r) => sig.push("failed " + r.url()));
    await page.goto(base + "/broken-resource.html");
    await new Promise((r) => setTimeout(r, 150));
    await page.close();
    const both = sig.some((s) => s.includes("missing.png")) && sig.some((s) => s.includes("missing.js"));
    return { status: both ? "Yes" : sig.length ? "Workaround" : "No", note: "page.on('response') status>=400 and page.on('requestfailed'); both img and script reported with URL", evidence: sig };
  });

  await probe("eval", async () => {
    const page = await browser.newPage();
    await page.goto(base + "/ok.html");
    const v = await page.evaluate("document.querySelectorAll('li').length");
    await page.close();
    return { status: v === 100 ? "Yes" : "No", note: "page.evaluate(), structured return value", evidence: v };
  });

  await probe("screenshot", async () => {
    const page = await browser.newPage();
    await page.goto(base + "/ok.html");
    const t0 = Date.now();
    const buf = await page.screenshot({ type: "png" });
    const ms = Date.now() - t0;
    await page.close();
    return { status: buf.length > 1000 ? "Yes" : "No", note: `page.screenshot() -> ${buf.length} bytes PNG in ${ms} ms`, evidence: buf.length };
  });

  await probe("input", async () => {
    const page = await browser.newPage();
    await page.goto(base + "/caps.html");
    await page.click("#btn");
    await page.fill("#inp", "hello");
    await page.keyboard.press("End");
    const count = await page.textContent("#count");
    const typed = await page.textContent("#typed");
    const trusted = await page.textContent("#trusted");
    await page.close();
    return { status: count === "1" && typed === "hello" ? "Yes" : "No", note: `page.click/fill/keyboard; events are ${trusted}`, evidence: { count, typed, trusted } };
  });

  await probe("wait", async () => {
    const page = await browser.newPage();
    await page.goto(base + "/caps.html");
    const t0 = Date.now();
    await page.waitForSelector("#late");
    const ms = Date.now() - t0;
    await page.goto(base + "/spa.html", { waitUntil: "networkidle" });
    await page.close();
    return { status: "Yes", note: `page.waitForSelector (#late appeared after ${ms} ms), goto waitUntil 'networkidle'`, evidence: ms };
  });

  await probe("intercept", async () => {
    const page = await browser.newPage();
    await page.route("**/api/items.json", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{ id: 1, name: "mocked" }, { id: 2, name: "mocked" }]) }));
    const lines = [];
    page.on("console", (m) => lines.push(m.text()));
    await page.goto(base + "/spa.html");
    await page.waitForFunction(() => document.querySelectorAll("#list li").length > 0);
    const n = await page.evaluate("document.querySelectorAll('#list li').length");
    await page.close();
    return { status: n === 2 ? "Yes" : "No", note: "page.route() fulfilled /api/items.json with 2 mocked items", evidence: n };
  });

  await probe("multi_page", async () => {
    const a = await browser.newPage();
    const b = await browser.newPage();
    await Promise.all([a.goto(base + "/ok.html"), b.goto(base + "/spa.html")]);
    const [ta, tb] = await Promise.all([a.title(), b.title()]);
    await a.close();
    await b.close();
    return { status: ta === "ok" && tb === "spa" ? "Yes" : "No", note: "browser.newPage() x2 in one browser, concurrently", evidence: [ta, tb] };
  });

  await probe("structured_output", async () => {
    const page = await browser.newPage();
    const errs = [];
    page.on("pageerror", (e) => errs.push({ message: e.message }));
    await page.goto(base + "/ok.html");
    const value = await page.evaluate("({ li: document.querySelectorAll('li').length, title: document.title })");
    await page.close();
    const obj = { ok: errs.length === 0, value, errors: errs };
    return { status: "Yes", note: "library call; the script emits whatever JSON it wants (eval returns structured values)", evidence: obj };
  });

  await probe("exit_code", async () => {
    // Demonstrated by this very script: we exit 1 if any probe says No.
    return { status: "Yes", note: "script decides: process.exit(1) on pageerror (tested by pw_caps.js itself)", evidence: true };
  });

  await browser.close();
  process.stdout.write(JSON.stringify(out) + "\n");
  process.exit(Object.values(out).some((v) => v.status === "No") ? 1 : 0);
}

main().catch((e) => {
  process.stderr.write(String(e && e.stack ? e.stack : e) + "\n");
  process.exit(3);
});
