"""
Record the Agent OS product walkthrough as SEVEN short, independent clips — one
per beat, each in its own fresh Playwright BrowserContext. Recording each beat
separately keeps every clip's video encoder from accumulating backlog (a single
long recording of many smooth scrolls stalls the browser), so every open/scroll
stays smooth and deterministic. demo/build/clips.json records, per clip, the
in-clip offset where the beat's *content* begins and how long it runs, so the
assembly step can trim each clip to exactly its content and drop all setup.

All read-only: GET navigation, modal opens, a smooth-scroll tour, and a LOCAL
(confirm:false) Git commit *preview* that is never confirmed. Private account
slugs (Vercel/Supabase/Stripe) are redacted live in the DOM; the public GitHub
org is kept.
"""
import json
import os
import time
from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
HERE = os.path.dirname(__file__)
RAW = os.path.join(HERE, "raw")
BUILD = os.path.join(HERE, "build")
for d in (RAW, BUILD):
    os.makedirs(d, exist_ok=True)

# 720p: recording at 900p backs the video encoder up during smooth scrolls of
# heavy content; 720p is a standard, readable demo resolution.
VIEW = {"width": 1280, "height": 720}

RUN_PLAN = "20260708-025224-22b4a96d"    # 7-task plan + 181-event trace
RUN_GREEN = "20260708-081514-8dbc6968"   # build + browser + visual all PASS
RUN_RECOVER = "20260708-071842-cbbc41eb"  # browser FAILED -> runtime recovery + git

REDACT = r"""
() => {
  if (window.__redactorOn) return;
  window.__redactorOn = true;
  const redact = () => {
    document.querySelectorAll('.integration-card').forEach(card => {
      const mark = card.querySelector('.integration-mark');
      const status = card.querySelector('.integration-status');
      if (!mark || !status) return;
      if (!mark.classList.contains('github') && /Connected/i.test(status.textContent||'')) {
        if (status.textContent.trim() !== 'Connected') status.textContent = 'Connected';
      }
    });
    document.querySelectorAll('.links-item').forEach(item => {
      const link = item.querySelector('.external-link');
      const label = ((link && link.textContent) || '').trim();
      if (/^Supabase/i.test(label)) { const s = item.querySelector('.run-chat-muted'); if (s) s.remove(); }
    });
    document.querySelectorAll('a.external-link').forEach(a => a.removeAttribute('title'));
  };
  redact();
  setInterval(redact, 250);
}
"""

SMOOTH_SCROLL = r"""
(args) => new Promise(res => {
  const {sel, target, duration} = args;
  const el = document.querySelector(sel);
  if (!el) { res(false); return; }
  const start = el.scrollTop, dist = target - start, t0 = performance.now();
  function step(now){
    const p = Math.min(1, (now - t0) / duration);
    const e = p < 0.5 ? 2*p*p : 1 - Math.pow(-2*p+2, 2)/2;
    el.scrollTop = start + dist * e;
    if (p < 1) requestAnimationFrame(step); else res(true);
  }
  requestAnimationFrame(step);
})
"""

TARGET_FOR_TEXT = r"""
(args) => {
  const {contSel, text, tags, offset} = args;
  const cont = document.querySelector(contSel);
  if (!cont) return null;
  const nodes = [...cont.querySelectorAll((tags||['h4']).join(','))];
  const h = nodes.find(x => (x.textContent||'').includes(text));
  if (!h) return null;
  const cr = cont.getBoundingClientRect(), hr = h.getBoundingClientRect();
  return cont.scrollTop + (hr.top - cr.top) - (offset||120);
}
"""


class Clip:
    """One beat's recording session; tracks the in-clip content window."""
    def __init__(self, page):
        self.page = page
        self.t0 = time.monotonic()
        self.start = None

    def now(self):
        return round(time.monotonic() - self.t0, 3)

    def begin(self):
        # content starts here — everything before is setup, trimmed off later
        self.start = self.now()

    def hold(self, secs):
        self.page.wait_for_timeout(int(secs * 1000))

    def scroll(self, sel, target, duration=1000):
        self.page.evaluate(SMOOTH_SCROLL, {"sel": sel, "target": float(target), "duration": duration})
        self.page.wait_for_timeout(duration + 40)

    def scroll_text(self, cont_sel, text, tags=None, offset=130, duration=1000):
        t = self.page.evaluate(TARGET_FOR_TEXT, {"contSel": cont_sel, "text": text, "tags": tags, "offset": offset})
        if t is None:
            self.page.wait_for_timeout(500)
            t = self.page.evaluate(TARGET_FOR_TEXT, {"contSel": cont_sel, "text": text, "tags": tags, "offset": offset})
        if t is None:
            print(f"      (text not found: {text!r})")
            return False
        self.scroll(cont_sel, max(0, t), duration)
        return True

    def click(self, js):
        return self.page.evaluate(js)

    def goto_ready(self):
        self.page.goto(BASE, wait_until="domcontentloaded")
        self.page.evaluate(REDACT)
        self.page.wait_for_selector(".conv-btn:not(.new-conv)", timeout=15000)

    def open_run(self, run_id):
        cond = """(rid)=>{const c=document.querySelector('.run-detail-runid');const hs=document.querySelectorAll('.run-detail-body h4');return c&&(c.textContent||'').includes(rid)&&hs.length>=4;}"""
        self.click(f"() => {{ const b=document.querySelector('.run-row[title=\"{run_id}\"]'); if(b) b.click(); }}")
        self.page.wait_for_selector(".run-detail-modal", timeout=8000)
        self.page.wait_for_function(cond, arg=run_id, timeout=10000)
        self.page.evaluate("() => { const b=document.querySelector('.run-detail-body'); if(b) b.scrollTop=0; }")
        self.page.wait_for_timeout(500)


# ------------------------------- beats ---------------------------------------

def beat1_cockpit(c: Clip):
    """Landing cockpit: memory, integrations, connectors, real Runs list."""
    c.goto_ready()
    c.page.wait_for_timeout(2200)  # let connector statuses resolve + redact
    c.begin()
    c.hold(2.4)
    rp = c.page.evaluate("() => { const el=document.querySelector('.context-panel'); return el? el.scrollHeight: 0; }")
    c.scroll(".context-panel", rp, duration=900)
    c.hold(2.0)


def beat2_thread(c: Clip):
    """The chat thread: Main Agent plans + hands off, Coding Agent executes."""
    c.goto_ready()
    c.click("() => { const b=document.querySelector('.conv-btn:not(.new-conv)'); if(b) b.click(); }")
    c.page.wait_for_selector(".chat-messages", timeout=10000)
    c.page.wait_for_timeout(1300)
    c.begin()
    c.scroll_text(".chat-messages", "hand off", tags=["div", "p"], offset=80, duration=1000)
    c.hold(2.3)
    c.scroll_text(".chat-messages", "Coding Agent is running", tags=["p", "strong", "div"], offset=100, duration=1000)
    c.hold(1.9)


def beat3_trace(c: Clip):
    """Execution trace: 7-task plan graph + the audited 181-event timeline."""
    c.goto_ready()
    c.page.wait_for_timeout(600)
    c.open_run(RUN_PLAN)
    c.begin()
    c.hold(1.8)  # plan & tasks (some completed, some failed)
    c.scroll_text(".run-detail-body", "Timeline", tags=["h4"], offset=110, duration=1000)
    c.hold(2.6)


def beat4_verify(c: Clip):
    """Verification: real build + browser + visual review, all PASSED."""
    c.goto_ready()
    c.page.wait_for_timeout(600)
    c.open_run(RUN_GREEN)
    c.begin()
    c.hold(2.2)  # timeline: verification/browser/visual all passed
    c.scroll_text(".run-detail-body", "Browser Verification", tags=["h4"], offset=120, duration=1000)
    c.hold(2.4)


def beat5_recovery(c: Clip):
    """Failure -> evidence -> bounded recovery (typed, linked child run)."""
    c.goto_ready()
    c.page.wait_for_timeout(600)
    c.open_run(RUN_RECOVER)
    c.begin()
    c.scroll_text(".run-detail-body", "Browser Verification", tags=["h4"], offset=120, duration=1000)
    c.hold(1.9)  # the failing dev-server evidence (unknown option --host)
    c.scroll_text(".run-detail-body", "Recovery", tags=["h4"], offset=140, duration=1000)
    c.hold(2.6)  # diagnosis + proposed fix + recovery run dispatched


def beat6_approval(c: Clip):
    """External actions require explicit approval: a Git commit contract."""
    c.goto_ready()
    c.page.wait_for_timeout(600)
    c.open_run(RUN_RECOVER)
    c.scroll_text(".run-detail-body", "Project Ops", tags=["h4"], offset=110, duration=900)
    c.begin()
    c.hold(1.2)  # commit / push / PR / rollback contract buttons
    # LOCAL, non-mutating commit *preview* (confirm:false) — never confirmed.
    c.click("""() => {
        const m=document.querySelector('.run-detail-modal'); if(!m) return;
        const b=[...m.querySelectorAll('.gitops-btn')].find(x=>/Commit/i.test(x.textContent||''));
        if(b) b.click();
    }""")
    try:
        c.page.wait_for_selector(".run-detail-modal .gitops-contract", timeout=9000)
        c.page.wait_for_timeout(300)
        c.page.locator(".run-detail-modal .gitops-contract").first.scroll_into_view_if_needed()
    except Exception as e:
        print("      (commit contract:", e, ")")
    c.hold(2.6)  # the preview/confirm contract card (Create commit / Cancel)


def beat7_result(c: Clip):
    """The finished, shipped Pulseboard — real browser-verification captures."""
    c.goto_ready()
    c.begin()
    for name in ["view-02.png", "view-03.png"]:
        url = f"{BASE}/api/projects/sample-project/execution/runs/{RUN_GREEN}/screenshot?name={name}"
        c.page.goto(url, wait_until="networkidle")
        c.page.wait_for_timeout(2100)


BEATS = [
    ("b1_cockpit", beat1_cockpit),
    ("b2_thread", beat2_thread),
    ("b3_trace", beat3_trace),
    ("b4_verify", beat4_verify),
    ("b5_recovery", beat5_recovery),
    ("b6_approval", beat6_approval),
    ("b7_result", beat7_result),
]


def main():
    manifest = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--force-color-profile=srgb"])
        for name, fn in BEATS:
            ctx = browser.new_context(
                viewport=VIEW, device_scale_factor=1,
                record_video_dir=RAW, record_video_size=VIEW,
            )
            page = ctx.new_page()
            c = Clip(page)
            fn(c)
            end = c.now()
            page.wait_for_timeout(200)
            video = page.video
            ctx.close()  # flush webm
            clip_path = video.path()
            dur = round(end - (c.start or 0.0), 3)
            manifest.append({
                "name": name,
                "clip": os.path.basename(clip_path),
                "start": round(c.start or 0.0, 3),
                "dur": dur,
            })
            print(f"  {name:12s} clip={os.path.basename(clip_path)}  content=[{c.start:.2f}s, +{dur:.2f}s]")
        browser.close()
    with open(os.path.join(BUILD, "clips.json"), "w", encoding="utf-8") as f:
        json.dump({"view": VIEW, "clips": manifest}, f, indent=2)
    total = sum(m["dur"] for m in manifest)
    print(f"\nTotal beat content: {total:.1f}s across {len(manifest)} clips")
    print("wrote build/clips.json")


if __name__ == "__main__":
    main()
