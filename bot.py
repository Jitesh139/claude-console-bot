"""Claude platform login bot.

Setup (Windows, Google Chrome installed):  pip install playwright

  python bot.py add <name> [<name> ...]   one-time: opens a real Chrome per name, sign into Google in each, close them
  python bot.py [<name> ...]              given (default: every) saved account in parallel: login (only if needed) -> screenshot dashboard
                                          accounts stay signed in, so later runs skip Google entirely
  python bot.py claim <link>              every account in info.txt in parallel: open link, fill surname if empty, tick uses, submit

info.txt, one line per account:  acc1: surname=baghel; uses=Learning, Building agents, Coding
"""
import re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

URL = "https://platform.claude.com/login"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
ROOT = Path(__file__).parent
PROFILES, SHOTS = ROOT / "profiles", ROOT / "screenshots"


def on_dashboard(url):
    u = urlparse(url)
    host_ok = u.hostname and (u.hostname.endswith("claude.com") or u.hostname.endswith("anthropic.com"))
    return bool(host_ok) and not re.match(r"/(login|auth|oauth|api)", u.path)


def add(names):
    # Plain Chrome, not automated: Google blocks sign-in from automation-controlled browsers.
    PROFILES.mkdir(exist_ok=True)
    procs = {n: subprocess.Popen([CHROME, f"--user-data-dir={PROFILES / n}", "https://accounts.google.com"]) for n in names}
    for n, proc in procs.items():
        proc.wait()
        print(f"saved profile '{n}'")


def run_isolated(profile, claim_args=None):
    with sync_playwright() as p:  # one Playwright per thread: the sync API can't be shared across threads
        run_one(p, profile, claim_args)


def read_info(text):
    info = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        f = dict(kv.strip().split("=", 1) for kv in rest.split(";"))
        info[name.strip()] = (f["surname"].strip(), [u.strip() for u in f["uses"].split(",") if u.strip()])
    return info


# Related words per use, so "Coding" also finds e.g. "Software development". Add words here if the form uses others.
RELATED = {
    "learning": "learn study education educational research experiment explore personal course student",
    "building agents": "agent agents agentic automation automate workflow assistant bot api sdk",
    "coding": "code coding software development developer programming app apps engineering tool api sdk",
}


def best_box(use, labels, taken):
    # Pick the label sharing the most words with the use (+ its related words); None if nothing relates.
    words = set(re.findall(r"[a-z]+", (use + " " + RELATED.get(use.lower(), "")).lower()))
    score = lambda l: sum(any(w.startswith(k[:5]) for k in words) for w in re.findall(r"[a-z]+", (l or "").lower()))
    best = max((i for i in range(len(labels)) if i not in taken), key=lambda i: score(labels[i]), default=None)
    return best if best is not None and score(labels[best]) > 0 else None


def pick_boxes(uses, labels):
    # Related matches first; each use with no related box then takes the next free box in form order.
    # shortcut: fallback skips "enterprise" (these are individual accounts); add other words to skip if the form needs it
    taken, unmatched = [], []
    for u in uses:
        i = best_box(u, labels, set(taken))
        (taken.append(i) if i is not None else unmatched.append(u))
    free = [i for i in range(len(labels)) if i not in taken and "enterpri" not in (labels[i] or "").lower()]
    return taken + free[:len(unmatched)]


def claim(ctx, page, link, surname, uses):
    # shortcut: field/checkbox/button found by visible text, written before seeing the real form; adjust once we have the link
    page.goto(link, wait_until="domcontentloaded")
    if "/login" in page.url:  # session expired: log in, then back to the link
        login(ctx, page)
        page.goto(link, wait_until="domcontentloaded")
    submit = page.get_by_role("button", name=re.compile(r"submit|apply|claim", re.I)).first
    submit.wait_for(timeout=20000)
    field = page.get_by_label(re.compile(r"surname|last name", re.I)).first
    if field.count() and not field.input_value().strip():  # already filled = leave it
        field.fill(surname)
    boxes = page.get_by_role("checkbox")
    labels = boxes.evaluate_all("els => els.map(e => e.labels?.[0]?.innerText || e.getAttribute('aria-label') || e.closest('label')?.innerText || '')")
    picks = pick_boxes(uses, labels)
    if not picks:
        raise LookupError(f"no usable checkbox; the form has: {labels}")
    for i in picks:
        boxes.nth(i).check(timeout=3000)
    print(f"ticked: {[labels[i] for i in picks]}")
    submit.click()  # clicked once, never retried: a retry could submit twice
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass  # screenshot shows the result either way


def login(ctx, page):
    # Straight sequence of waits, no polling: polling 4 browsers every 100ms starved the PC and re-clicked buttons.
    page.goto(URL, wait_until="domcontentloaded")
    google = page.get_by_role("button", name=re.compile("google", re.I)).first
    google.or_(page.get_by_text("Organization credits")).first.wait_for(timeout=15000)
    if on_dashboard(page.url):  # still logged in from last run: the fast path
        return
    try:
        with ctx.expect_page(timeout=15000) as popup:
            google.click(timeout=15000)
    except Exception:
        page.wait_for_url(on_dashboard, timeout=5000)  # redirect to dashboard raced the button
        return
    g = popup.value
    tile = g.locator("[data-identifier], [data-email]").first
    cont = g.get_by_role("button", name=re.compile(r"^(Continue|Allow|Next)$", re.I)).first
    try:
        tile.or_(cont).first.wait_for(timeout=20000)  # timeout here = password/2FA page, finish it by hand
        if tile.is_visible():
            tile.click(no_wait_after=True)  # no_wait_after: a plain click blocks ~30s on the popup's navigation
        cont.click(timeout=20000, no_wait_after=True)
    except Exception:
        if not g.is_closed():  # popup closing on its own = Google skipped the Continue screen
            raise
    page.wait_for_url(on_dashboard, timeout=30000)


def run_one(p, profile, claim_args=None):
    t0 = time.time()
    ctx = p.chromium.launch_persistent_context(
        profile, channel="chrome", headless=False, no_viewport=True,
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled", "--start-maximized",
              # 4 stacked windows: Chrome throttles the hidden ones, which froze logins
              "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
              "--disable-background-timer-throttling"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    SHOTS.mkdir(exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    try:
        if claim_args:
            claim(ctx, page, *claim_args)
            shot = SHOTS / f"{profile.name}_{stamp}_CLAIM.png"
            page.screenshot(path=shot, full_page=True)
            print(f"[{profile.name}] {time.time() - t0:.1f}s submitted -> {shot}")
            return
        login(ctx, page)
        try:
            page.get_by_text("Organization credits").wait_for(timeout=5000)
        except Exception:
            pass  # layout changed; screenshot anyway
        shot = SHOTS / f"{profile.name}_{stamp}.png"
        page.screenshot(path=shot, full_page=True)
        print(f"[{profile.name}] {time.time() - t0:.1f}s screenshot -> {shot}")
        # no logout on purpose: staying signed in skips Google next run (fast, and avoids Google's "verify it's you")
    except Exception as e:
        print(f"[{profile.name}] FAILED after {time.time() - t0:.1f}s: {e}")
        for i, pg in enumerate(ctx.pages):  # show where it got stuck
            try:
                pg.screenshot(path=SHOTS / f"{profile.name}_{stamp}_FAILED_{i}.png")
            except Exception:
                pass
    finally:
        ctx.close()


def main():
    assert on_dashboard("https://platform.claude.com/dashboard")
    assert not on_dashboard("https://platform.claude.com/login?x=1")
    assert not on_dashboard("https://accounts.google.com/o/oauth2")

    form = ["Personal projects and learning", "Building AI agents and automation", "Software development", "Customer support"]
    assert [best_box(u, form, set()) for u in ["Learning", "Building agents", "Coding"]] == [0, 1, 2]
    assert best_box("Coding", ["Marketing", "Sales"], set()) is None
    uses = ["Learning", "Building agents", "Coding"]
    assert pick_boxes(uses, ["Claude API", "Enterprise", "Claude", "Claude Code"]) == [0, 3, 2]
    assert pick_boxes(uses, ["Enterprise", "Claude"]) == [1]
    assert read_info("acc1: surname=x y; uses=A, B ,") == {"acc1": ("x y", ["A", "B"])}

    if len(sys.argv) >= 3 and sys.argv[1] == "add":
        return add(sys.argv[2:])
    if len(sys.argv) == 3 and sys.argv[1] == "claim":
        info = read_info((ROOT / "info.txt").read_text())
        missing = [n for n in info if not (PROFILES / n).is_dir()]
        if missing:
            sys.exit(f"no saved profile for {missing}: python bot.py add {' '.join(missing)}")
        with ThreadPoolExecutor(max_workers=len(info)) as pool:
            list(pool.map(lambda n: run_isolated(PROFILES / n, (sys.argv[2], *info[n])), info))
        return
    profiles = sorted(d for d in PROFILES.glob("*") if d.is_dir()) if PROFILES.exists() else []
    if sys.argv[1:]:
        profiles = [d for d in profiles if d.name in sys.argv[1:]]
    if not profiles:
        sys.exit("no accounts yet: python bot.py add <name> [<name> ...]")
    # shortcut: 4 Chrome windows at once; raise if the PC handles more, lower if Google starts showing captchas
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run_isolated, profiles))


if __name__ == "__main__":
    main()
