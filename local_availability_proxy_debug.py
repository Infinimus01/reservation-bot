import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

TICKETS_URL = "https://resa.notredamedeparis.fr/en/reservationindividuelle/tickets"

OUT_DIR = Path("local_debug")
OUT_DIR.mkdir(exist_ok=True)

def is_challenge(html: str) -> bool:
    return any(x in html for x in ["Please enable JS", "var dd=", 'id="cmsg"', "api-js.datadome.co"])

def load_first_proxy():
    path = Path("proxies.txt")
    if not path.exists():
        raise RuntimeError("proxies.txt not found")

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split(":")
        if len(parts) < 4:
            raise RuntimeError(f"Bad proxy format: {line}")

        host, port, username = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        return {
            "server": f"http://{host}:{port}",
            "username": username,
            "password": password,
            "display": f"{host}:{port}:{username}:***",
        }

    raise RuntimeError("No proxy found in proxies.txt")

async def main():
    proxy = load_first_proxy()
    print("Using proxy:", proxy["display"])

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            slow_mo=400,
            proxy={
                "server": proxy["server"],
                "username": proxy["username"],
                "password": proxy["password"],
            },
        )
        context = await browser.new_context(
            locale="en-US",
            viewport={"width": 1365, "height": 768},
        )
        page = await context.new_page()

        print("Checking proxy IP...")
        try:
            await page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=60000)
            print("ipify:", await page.text_content("body"))
        except Exception as e:
            print("ipify failed:", repr(e))

        print("\nOpening Notre-Dame /tickets")
        await page.goto(TICKETS_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(15000)

        html = await page.content()
        (OUT_DIR / "01_proxy_tickets.html").write_text(html, encoding="utf-8")
        await page.screenshot(path=str(OUT_DIR / "01_proxy_tickets.png"), full_page=True)

        print("title:", await page.title())
        print("url:", page.url)
        print("has_form:", "<form" in html.lower())
        print("has_csrf:", "csrf_name" in html)
        print("has_token_tickets:", "token_tickets" in html)
        print("has_challenge:", is_challenge(html))
        print("cookies:", [c["name"] for c in await context.cookies()])

        if is_challenge(html) or "csrf_name" not in html:
            print("RESULT: BLOCKED AT /tickets")
            input("Browser visible hai. Inspect karo, phir Enter...")
            await browser.close()
            return

        print("\nSelecting 1 ticket via JS hidden select")
        await page.evaluate("""
            () => {
                const sel = document.querySelector('select[name^="tickets["]');
                if (!sel) throw new Error("ticket select not found");
                sel.value = "1";
                sel.dispatchEvent(new Event("change", { bubbles: true }));
                if (window.jQuery) {
                    window.jQuery(sel).val("1").trigger("change");
                }
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

        await page.wait_for_timeout(15000)

        html = await page.content()
        (OUT_DIR / "02_proxy_date.html").write_text(html, encoding="utf-8")
        await page.screenshot(path=str(OUT_DIR / "02_proxy_date.png"), full_page=True)

        print("title:", await page.title())
        print("url:", page.url)
        print("date has_calendar:", "ticketMinDate" in html or "ticketMaxDate" in html or "datepicker" in html)
        print("date has_challenge:", is_challenge(html))
        print("cookies:", [c["name"] for c in await context.cookies()])

        if is_challenge(html):
            print("RESULT: BLOCKED AT /date")
        else:
            print("RESULT: CALENDAR PAGE REACHED")

        input("Browser visible hai. Inspect karo, phir Enter...")
        await browser.close()

asyncio.run(main())
