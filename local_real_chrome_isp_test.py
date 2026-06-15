import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

TICKETS_URL = "https://resa.notredamedeparis.fr/en/reservationindividuelle/tickets"
PROFILE_DIR = str(Path.home() / "ndame-real-chrome-profile")

PROXY = {
    "server": "http://217.67.73.70:12323",
    "username": "14a577cb42b4a",
    "password": "73c000cdd6",
}

def is_challenge(html: str) -> bool:
    return any(x in html for x in [
        "Verification Required",
        "Slide right to secure your access",
        "Access is temporarily restricted",
        "Please enable JS",
        "var dd=",
        'id="cmsg"',
        "api-js.datadome.co",
    ])

async def main():
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            PROFILE_DIR,
            channel="chrome",
            headless=False,
            proxy=PROXY,
            viewport={"width": 1365, "height": 768},
            locale="en-US",
            slow_mo=250,
            args=[
                "--start-maximized",
                "--disable-dev-shm-usage",
            ],
        )

        page = context.pages[0] if context.pages else await context.new_page()

        print("Checking IP...")
        try:
            await page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=60000)
            print("ipify:", await page.text_content("body"))
        except Exception as e:
            print("ipify failed:", repr(e))

        print("Opening tickets...")
        await page.goto(TICKETS_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(15000)

        html = await page.content()
        print("title:", await page.title())
        print("url:", page.url)
        print("has_form:", "<form" in html.lower())
        print("has_csrf:", "csrf_name" in html)
        print("has_challenge:", is_challenge(html))
        print("cookies:", [c["name"] for c in await context.cookies()])

        if is_challenge(html) or "csrf_name" not in html:
            print("If slider appears, solve it manually. Then press Enter here.")
            input("Press Enter after manual check/solve...")
            await page.wait_for_timeout(5000)
            html = await page.content()
            print("after has_csrf:", "csrf_name" in html)
            print("after has_challenge:", is_challenge(html))

        if "csrf_name" not in html or is_challenge(html):
            print("RESULT: BLOCKED AT /tickets")
            input("Enter to close...")
            await context.close()
            return

        print("Setting ticket via JS")
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

        await page.wait_for_timeout(15000)
        html = await page.content()

        print("date title:", await page.title())
        print("date url:", page.url)
        print("date has_calendar:", "ticketMinDate" in html or "ticketMaxDate" in html or "datepicker" in html)
        print("date has_challenge:", is_challenge(html))

        if is_challenge(html):
            print("RESULT: BLOCKED AT /date")
        else:
            print("RESULT: CALENDAR PAGE REACHED ✅")

        input("Inspect, then Enter to close...")
        await context.close()

asyncio.run(main())
