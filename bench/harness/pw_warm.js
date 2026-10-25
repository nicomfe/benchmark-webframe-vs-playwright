// Configuration pw-<engine>-warm: one long-lived node process, browser
// launched once, a new page per check. Requests arrive as JSON lines on
// stdin, responses leave as JSON lines on stdout. The runner times the
// round trip from writing the request to reading the response, so the timer
// includes IPC but not browser launch.
//
// Usage: node pw_warm.js <engine>
// First line printed is {"ready":true,"launchMs":...}.
"use strict";

const readline = require("node:readline");
const { runCheck, launchOptions, getEngine, now } = require("./pw_check");

async function main() {
  const engineName = process.argv[2];
  const engine = getEngine(engineName);
  const t0 = now();
  const browser = await engine.launch(launchOptions(engineName));
  process.stdout.write(JSON.stringify({ ready: true, launchMs: now() - t0, version: browser.version() }) + "\n");

  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  // Requests are processed strictly one at a time, in order.
  let chain = Promise.resolve();
  rl.on("line", (line) => {
    if (!line.trim()) return;
    chain = chain.then(async () => {
      let req;
      try {
        req = JSON.parse(line);
      } catch (e) {
        process.stdout.write(JSON.stringify({ ok: false, errors: [{ type: "harness", text: "bad json: " + e }] }) + "\n");
        return;
      }
      if (req.cmd === "quit") {
        await browser.close();
        process.exit(0);
      }
      const res = await runCheck(browser, req);
      process.stdout.write(JSON.stringify(res) + "\n");
    });
  });
  rl.on("close", async () => {
    await chain;
    try {
      await browser.close();
    } catch (_) {}
    process.exit(0);
  });
}

main().catch((e) => {
  process.stderr.write(String(e && e.stack ? e.stack : e) + "\n");
  process.exit(3);
});
