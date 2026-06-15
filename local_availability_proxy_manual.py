import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

TICKETS_URL = "https://resa.notredamedeparis.fr/en/reservationindividuelle/tickets"
OUT_DIR = Path("local_debug")
OUT_DIR.mkdir(exist_ok=True)

def is_challenge(html: str) -> bool:
    return any(x in html for x in [
        "Verification Required",
        "Slide right to secure your access",
        "Please enable JS",
        "var dd=",
        'id="cmsg"',
        "api-js.datadome.co",
    ])

def load_proxies(limit=10):
    proxies = []
    for line in Path("proxies.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 4:
            continue
        host, port, username = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        proxies.append({
            "server": f"http://{host}:{port}",
            "username": username,
            "password": password,
            "display": f"{host}:{port}:{username}:***",
        })
        if len(proxies) >= limit:
            break
    return proxies

async def test_proxy(p, proxy, index):
    print(f"\n=== TEST PROXY {index}: {proxy['display']} ===")

    browser = await p.chromium.launch(
        headless=False,
        slow_mo=250,
        proxy={
            "server": proxy["server"],
            "username": proxy["username"],
            "password": proxy["password"],
        },
    )

    context = await browser.new_context(locale="en-US", viewport={"width": 1365, "height": 768})
    page = await context.new_page()

    try:
        print("Checking proxy IP...")
        try:
            await page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=45000)
            print("ipify:", await page.text_content("body"))
        except Exception as e:
            print("ipify failed:", repr(e))

        print("Opening /tickets")
        await page.goto(TICKETS_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(10000)

        html = await page.content()
        (OUT_DIR / f"proxy_{index}_tickets.html").write_text(html, encoding="utf-8")
        await page.screenshot(path=str(OUT_DIR / f"proxy_{index}_tickets.png"), full_page=True)

        print("title:", await page.title())
        print("url:", page.url)
        print("has_form:", "<form" in html.lower())
        print("has_csrf:", "csrf_name" in html)
        print("has_challenge:", is_challenge(html))
        print("cookies:", [c["name"] for c in await context.cookies()])

        if is_challenge(html) or "csrf_name" not in html:
            print("Manual verification may be needed. If browser shows slider, solve it now.")
            input("After solving / checking, press Enter...")

            await page.wait_for_timeout(5000)
            html = await page.content()

            print("after manual has_csrf:", "csrf_name" in html)
            print("after manual has_challenge:", is_challenge(html))

            if is_challenge(html) or "csrf_name" not in html:
                print("RESULT: still blocked at /tickets")
                await browser.close()
                return False

        print("Setting ticket select via JS")
        await page.evaluate("""
            () => {
                const sel = document.querySelector('select[name^="tickets["]');
                if (!sel) throw new Error("ticket select not found");
                sel.value = "1";
                sel.dispatchEvent(new Event("change", { bubbles: true }));
                if (window.jQuery) window.jQuery(sel).val("1").trigger("change");
            }
        """)
        await page.wait_for_timeout(1000)

        print("Submitting to /date")
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=90000):
                await page.locator("form").evaluate("(form) => form.submit()")
        except Exception as e:
            print("navigation warning:", repr(e))
            await page.wait_for_timeout(10000)

        await page.wait_for_timeout(10000)
        html = await page.content()

        (OUT_DIR / f"proxy_{index}_date.html").write_text(html, encoding="utf-8")
        await page.screenshot(path=str(OUT_DIR / f"proxy_{index}_date.png"), full_page=True)

        print("date title:", await page.title())
        print("date url:", page.url)
        print("date has_calendar:", "ticketMinDate" in html or "ticketMaxDate" in html or "datepicker" in html)
        print("date has_challenge:", is_challenge(html))

        if is_challenge(html):
            print("RESULT: BLOCKED AT /date")
            input("Inspect, then Enter...")
            await browser.close()
            return False

        print("RESULT: CALENDAR PAGE REACHED ✅")
        input("Inspect, then Enter...")
        await browser.close()
        return True

    except Exception as e:
        print("PROXY TEST FAILED:", repr(e))
        await browser.close()
        return False

async def main():
    proxies = load_proxies(limit=10)
    print("Loaded proxies:", len(proxies))
    if not proxies:
        raise RuntimeError("No proxies found in proxies.txt")

    async with async_playwright() as p:
        for i, proxy in enumerate(proxies, 1):
            ok = await test_proxy(p, proxy, i)
            if ok:
                print("Working proxy found.")
                return
        print("No proxy reached calendar.")

asyncio.run(main())
