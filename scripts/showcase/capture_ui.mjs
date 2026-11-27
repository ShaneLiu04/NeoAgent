// Records README showcase frames + stills from a live NeoAgent Studio (headless Chrome via CDP).
// Usage: node capture_ui.mjs <base-url> <out-dir>
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { launch, Recorder } from "./cdp.mjs";

const BASE = process.argv[2] || "http://127.0.0.1:8765";
const OUT = process.argv[3] || "showcase-out";
mkdirSync(join(OUT, "stills"), { recursive: true });

const page = await launch({ width: 1600, height: 960, port: 9335 });
const hideToast = () => page.eval(`document.getElementById("toast").classList.add("hidden")`);
const still = async (name, { keepToast = false } = {}) => {
  if (!keepToast) await hideToast();
  await page.screenshot(join(OUT, "stills", `${name}.png`));
};
const typeInto = async (rec, selector, text, step = 2) => {
  for (let i = step; i < text.length + step; i += step) {
    await page.eval(`(() => { const el = document.querySelector(${JSON.stringify(selector)});
      el.focus(); el.value = ${JSON.stringify(text)}.slice(0, ${i});
      el.dispatchEvent(new Event("input", {bubbles: true})); })()`);
    if (rec) await rec.frame();
  }
};
const view = async (tab) => {
  await page.click(`.nav-item[data-tab="${tab}"]`);
  await page.sleep(700);
};
const scrollTop = (sel) => page.eval(`document.querySelector(${JSON.stringify(sel)}).scrollTop = 0`);
const chatIdle = `!document.getElementById("chat-send").disabled`;
const loopIdle = `!document.querySelector("#ld-last-msg .spinner")`;

try {
  await page.goto(`${BASE}/?theme=light`, 1200);
  await page.waitFor(`document.getElementById("ws-name").textContent !== "—"`);
  await page.sleep(500);
  await still("01-home");

  // ── Scene 1: Agent session — streaming reasoning, folding, review ───────────
  {
    const rec = new Recorder(page, join(OUT, "chat"));
    await rec.frame(900);
    await typeInto(rec, "#chat-input", "检查当前仓库的 Harness L1-L7 与隐私合规状态", 3);
    await rec.frame(300);
    await page.eval(`document.getElementById("chat-form").requestSubmit()`);
    await rec.until(chatIdle, 110);
    await rec.frame(1500);
    await page.click('#role-seg .seg-item[data-role="reviewer"]');
    await page.sleep(250);
    await typeInto(rec, "#chat-input", "检视当前 Git Diff 的安全与代码规范问题", 3);
    await page.eval(`document.getElementById("chat-form").requestSubmit()`);
    await rec.until(chatIdle, 110);
    await page.eval(`document.querySelector(".msg-ai:last-child details.thought").open = true`);
    await page.sleep(300);
    await rec.frame(2600);
    rec.save();
    await still("02-chat-reasoning");
    await page.eval(`document.querySelector(".msg-ai:last-child details.thought").open = false`);
    await page.sleep(200);
    await still("02-chat");
  }

  // ── Scene 2: Closed loop — sheet, autopilot self-heal, feedback ──────────────
  {
    const rec = new Recorder(page, join(OUT, "loop"));
    await view("loops");
    await rec.frame(700);
    await page.click("#btn-open-new-modal");
    await page.sleep(400);
    await rec.frame(400);
    await typeInto(rec, "#nl-title", "Add apply_discount pricing calculation", 3);
    await page.click('#nl-mode .seg-item[data-mode="unattended"]');
    await page.sleep(250);
    await rec.frame(900);
    await still("03-new-loop-sheet");
    await page.click("#btn-create-loop");
    await page.waitFor(`!document.getElementById("loop-detail").classList.contains("hidden")`);
    await page.sleep(500);
    await rec.frame(1300);
    await page.click('.btn-accent[data-laction="autopilot"]');
    await page.sleep(120);
    await rec.until(loopIdle, 220, 180000);
    await page.sleep(500);
    await rec.frame(2200);
    await page.click('#insp-artifacts .artifact[data-art="fix-brief.md"]');
    await page.sleep(500);
    await rec.frame(1800);
    await still("04-loop-autopilot");

    const fb = [
      { id: "fb-1", text: "coupon above 100% crashes checkout with a 500 error", severity: "critical", version: "v0.1.0", reach: 5 },
      { id: "fb-2", text: "coupon above 100% gives negative totals and a 500 error", severity: "high", version: "v0.1.0", reach: 4 },
      { id: "fb-3", text: "please add bulk CSV export for invoices feature request", severity: "medium", reach: 6 },
    ].map((r) => JSON.stringify(r)).join("\n");
    await page.eval(`document.getElementById("fb-input").scrollIntoView({block: "center"})`);
    await page.eval(`document.getElementById("fb-input").value = ${JSON.stringify(fb)}`);
    await page.sleep(300);
    await rec.frame(1300);
    await page.click("#btn-submit-feedback");
    await page.sleep(100);
    await rec.until(loopIdle, 200, 60000);
    await page.sleep(400);
    await scrollTop("#view-loops .scroll-body");
    await page.sleep(300);
    await rec.frame(2800);
    rec.save();
    await still("05-loop-done");
    // Governed Review Protocol card: verdict, attestation and attribution for the latest round.
    await page.eval(`document.getElementById("ld-protocol-group")?.scrollIntoView({block: "center"})`);
    await page.sleep(400);
    await still("05-protocol");
  }

  // ── Scene 2b: Spec-to-Code Blueprint — synthesize, gate, isolated TDD, S/D/C review, acceptance ──
  {
    const rec = new Recorder(page, join(OUT, "blueprint"));
    const bpText = `document.getElementById("ld-blueprint")?.innerText || ""`;
    await view("loops");
    await page.click("#btn-open-new-modal");
    await page.sleep(400);
    await typeInto(rec, "#nl-title", "Add tiered volume discount", 3);
    await page.eval(`document.getElementById("nl-files").value = "app/calculator.py, tests/test_calc.py"`);
    await typeInto(rec, "#nl-reqs", "Orders of 100+ units get a 10% volume discount; A discount never makes the total negative", 6);
    await rec.frame(700);
    await page.click("#btn-create-loop");
    await page.waitFor(`!document.getElementById("loop-detail").classList.contains("hidden")`);
    await page.sleep(500);
    await rec.frame(900);
    await page.click('[data-laction="blueprint"]');
    await rec.until(`${loopIdle} && ${bpText}.includes("PLAN")`, 200, 60000);
    await page.eval(`document.getElementById("ld-blueprint-group").scrollIntoView({block: "start"})`);
    await page.sleep(500);
    await rec.frame(2200);
    await still("12-blueprint-synth");
    await page.click('[data-bp-act="next_task"]');
    await rec.until(`${loopIdle} && ${bpText}.includes("GREEN")`, 200, 60000);
    await page.sleep(500);
    await rec.frame(1800);
    await page.click('[data-bp-act="sdc"]');
    await rec.until(`${loopIdle} && ${bpText}.includes("SDC")`, 200, 60000);
    await page.sleep(500);
    await rec.frame(1200);
    await page.click('[data-bp-act="acceptance"]');
    await rec.until(`${loopIdle} && ${bpText}.includes("complete")`, 200, 60000);
    await page.sleep(600);
    await page.eval(`document.getElementById("ld-blueprint-group").scrollIntoView({block: "start"})`);
    await page.sleep(400);
    await rec.frame(2800);
    rec.save();
    await still("12-blueprint");
    await page.eval(`document.querySelector('.artifact[data-art="blueprint/spec.md"]')?.scrollIntoView({block: "center"})`);
    await page.click('.artifact[data-art="blueprint/spec.md"]');
    await page.sleep(600);
    await still("12-blueprint-spec");
  }

  // ── Scene 3: Skill library ───────────────────────────────────────────────────
  {
    const rec = new Recorder(page, join(OUT, "skills"));
    await view("skills");
    await rec.frame(900);
    await page.click('#sk-stage .pill-tab[data-stage="review"]');
    await page.sleep(500);
    await rec.frame(1100);
    await page.click('#sk-stage .pill-tab[data-stage=""]');
    await typeInto(rec, "#sk-query", "review", 1);
    await page.click("#btn-filter-skills");
    await page.sleep(500);
    await rec.frame(700);
    await page.click("#skills-list [data-sname]");
    await page.sleep(600);
    await rec.frame(1600);
    await page.click('.ref-chip[data-sref="checklist.md"]');
    await page.sleep(500);
    await rec.frame(1400);
    await still("06-skills");
    await page.click("#btn-validate-skills");
    await page.sleep(700);
    await rec.frame(2400);
    rec.save();
    await still("06-skills-validate");
  }

  // ── Scene 4: Harness L1–L7, L7 evolution approval, privacy scan ─────────────
  {
    const rec = new Recorder(page, join(OUT, "harness"));
    await view("harness");
    await rec.frame(1300);
    await still("07-harness-before");
    await page.click("#btn-apply-scaffold");
    await page.sleep(700);
    await rec.burst(1300, 200);
    await rec.frame(900);
    await page.eval(`document.querySelector("[data-evo-approve]")?.scrollIntoView({block: "center"})`);
    await page.sleep(300);
    await rec.frame(1000);
    await page.click("[data-evo-approve]");
    await page.sleep(900);
    await rec.frame(1800);
    await scrollTop("#view-harness .scroll-body");
    await page.sleep(300);
    await still("07-harness");
    await view("insights");
    await rec.frame(1000);
    await page.click("#btn-scan-privacy");
    await page.waitFor(`document.querySelector("#privacy-scan-result h3").textContent.includes("0 处")`, 60000);
    await page.sleep(400);
    await rec.frame(2600);
    rec.save();
    await still("08-insights", { keepToast: true });
  }

  // ── Dark appearance stills ──────────────────────────────────────────────────
  await page.click("#btn-theme");
  await view("loops");
  await page.sleep(500);
  await still("09-dark-loops");
  await view("compose");
  await page.sleep(500);
  await still("10-dark-chat");
  await view("harness");
  await page.sleep(500);
  await still("11-dark-harness");
  await view("loops");
  await page.eval(`document.getElementById("ld-blueprint-group")?.scrollIntoView({block: "start"})`);
  await page.sleep(500);
  await still("13-dark-blueprint");
  console.log("capture complete ->", OUT);
} finally {
  await page.close();
}
