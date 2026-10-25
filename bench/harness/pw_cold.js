// Configuration pw-<engine>-cold: a fresh `node` process per check that
// launches a new browser, performs one check, closes the browser and exits.
// This is what an agent that "shells out to a Playwright script" pays.
//
// Usage: node pw_cold.js <engine> '<request json>'
// Prints one JSON line with the response plus launch/close timings and
// exits 0 if the check reached a result (correctness is judged by the
// runner, not here), 3 on harness failure.
"use strict";

const { runCheck, launchOptions, getEngine, now } = require("./pw_check");

async function main() {
  const tProcess = now(); // process start is approximated by the runner via spawn time
  const engineName = process.argv[2];
  const req = JSON.parse(process.argv[3]);
  const t = {};

  let t0 = now();
  const engine = getEngine(engineName);
  t.requireMs = now() - t0; // time to load the playwright module

  t0 = now();
  const browser = await engine.launch(launchOptions(engineName));
  t.launchMs = now() - t0;

  const res = await runCheck(browser, req);

  t0 = now();
  await browser.close();
  t.browserCloseMs = now() - t0;

  res.t = { ...t, ...res.t, scriptMs: now() - tProcess };
  process.stdout.write(JSON.stringify(res) + "\n");
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ ok: false, errors: [{ type: "harness", text: String(e && e.stack ? e.stack : e) }] }) + "\n");
  process.exit(3);
});
