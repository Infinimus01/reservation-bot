import argparse
import os
import asyncio
import csv
import json
import re
from pathlib import Path
from urllib.parse import urlencode

import aiohttp

from playwright.async_api import async_playwright

BASE_URL = "https://resa.notredamedeparis.fr"
RESERVATION_URL = f"{BASE_URL}/en/reservationindividuelle/tickets"
TIMESLOTS_URL = f"{BASE_URL}/script/timeslots"
DEFAULT_PAYMENT_TURNSTILE_SITEKEY = "0x4AAAAAAA1IAg9Oedxa-RnI"


def proxy_display(proxy_line: str) -> str:
    parts = proxy_line.split(":")
    if len(parts) >= 4:
        return f"{parts[0]}:{parts[1]}:{parts[2]}:***"
    return proxy_line


def playwright_proxy_from_line(proxy_line: str):
    if not proxy_line:
        return None
    host, port, user, password = proxy_line.split(":", 3)
    return {
        "server": f"http://{host}:{port}",
        "username": user,
        "password": password,
    }


def load_lane(lane: str):
    csv_path = Path("/Users/amlendupandey/Downloads/ndame/proxy_lanes.csv")
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            if row["lane"] == lane:
                return row["proxy"], row["profile_dir"]
    raise SystemExit(f"Lane not found: {lane}")


async def post_form(page, url: str, fields: dict[str, str], timeout: int = 90000):
    await page.evaluate(
        """
        ({url, fields}) => {
            const form = document.createElement("form");
            form.method = "POST";
            form.action = url;
            form.style.display = "none";
            for (const [name, value] of Object.entries(fields)) {
                const input = document.createElement("input");
                input.type = "hidden";
                input.name = name;
                input.value = value;
                form.appendChild(input);
            }
            document.body.appendChild(form);
            form.submit();
        }
        """,
        {"url": url, "fields": fields},
    )
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        await page.wait_for_timeout(8000)


async def extract_ticket_fields(page, ticket_count: int):
    return await page.evaluate(
        """
        ({ticketCount}) => {
            const csrfName = document.querySelector('input[name="csrf_name"]')?.value || "";
            const csrfValue = document.querySelector('input[name="csrf_value"]')?.value || "";
            const tokenTickets = document.querySelector('input[name="token_tickets"]')?.value || "";
            const select = document.querySelector('select[name^="tickets["]');
            const ticketField = select?.getAttribute("name") || "tickets[411622]";
            return {
                csrfName,
                csrfValue,
                tokenTickets,
                ticketField,
                ticketCount: String(ticketCount)
            };
        }
        """,
        {"ticketCount": ticket_count},
    )


async def submit_tickets(page, ticket_count: int):
    fields = await extract_ticket_fields(page, ticket_count)
    if not fields["csrfName"] or not fields["csrfValue"]:
        raise RuntimeError("Tickets page CSRF missing")

    payload = {
        "csrf_name": fields["csrfName"],
        "csrf_value": fields["csrfValue"],
        "token_tickets": fields["tokenTickets"],
        fields["ticketField"]: str(ticket_count),
        "donation-input": "0",
        "donationCheck": "true",
    }

    print(f"Submitting tickets: {ticket_count}")
    await post_form(page, f"{BASE_URL}/en/reservationindividuelle/date", payload)
    await page.wait_for_timeout(8000)


async def fetch_timeslots(page, date_str: str, ticket_count: int):
    product_id = await page.evaluate(
        """
        () => {
            const input = document.querySelector('input[name^="ticketNumbers["]');
            const name = input?.getAttribute("name") || "ticketNumbers[411622]";
            const m = name.match(/ticketNumbers\\[(\\d+)\\]/);
            return m ? m[1] : "411622";
        }
        """
    )

    body = urlencode(
        {
            "tag": "notredame",
            "eventId": "1",
            "productEventId": "",
            "ticketDate": date_str,
            "ticketNumber": str(ticket_count),
            f"ticketNumbers[{product_id}]": str(ticket_count),
            "timeslotsGroup": "",
            "streetname": "reservationindividuelle",
        }
    )

    result = await page.evaluate(
        """
        async ({url, body}) => {
            const res = await fetch(url, {
                method: "POST",
                headers: {
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest"
                },
                body,
                credentials: "include"
            });
            return {status: res.status, text: await res.text()};
        }
        """,
        {"url": TIMESLOTS_URL, "body": body},
    )

    if int(result["status"]) >= 400:
        raise RuntimeError(f"Timeslots HTTP {result['status']}: {result['text'][:300]}")

    return json.loads(result["text"])


async def submit_calendar(page, date_str: str, time_str: str, ticket_count: int):
    data = await fetch_timeslots(page, date_str, ticket_count)
    slots = data.get("timeslots", {})
    matched = None

    for _, info in slots.items():
        if isinstance(info, dict) and info.get("time") == time_str:
            matched = info
            break

    if not matched:
        available = [v.get("time") for v in slots.values() if isinstance(v, dict) and v.get("active") and not v.get("soldOut")]
        raise RuntimeError(f"Target slot {date_str} {time_str} not available. Available={available}")

    total = int(matched.get("totalAvailable") or 0)
    if total < ticket_count:
        raise RuntimeError(f"Target slot has only {total}, need {ticket_count}")

    csrf = await page.evaluate(
        """
        () => ({
            csrfName: document.querySelector('input[name="csrf_name"]')?.value || "",
            csrfValue: document.querySelector('input[name="csrf_value"]')?.value || ""
        })
        """
    )
    if not csrf["csrfName"] or not csrf["csrfValue"]:
        raise RuntimeError("Calendar page CSRF missing")

    print(f"Submitting calendar: {date_str} {time_str}, available={total}")
    await post_form(
        page,
        f"{BASE_URL}/en/reservationindividuelle/personal-details",
        {
            "csrf_name": csrf["csrfName"],
            "csrf_value": csrf["csrfValue"],
            "ticketDate": date_str,
            "ticketTime": time_str,
        },
    )
    await page.wait_for_timeout(8000)


async def submit_details(page, args):
    csrf = await page.evaluate(
        """
        ({country}) => {
            const csrfName = document.querySelector('input[name="csrf_name"]')?.value || "";
            const csrfValue = document.querySelector('input[name="csrf_value"]')?.value || "";
            let countryCode = "US";
            const wanted = (country || "").trim().toLowerCase();
            const select = document.querySelector('select[name="country"]');
            if (select) {
                for (const opt of select.querySelectorAll("option")) {
                    const text = (opt.textContent || "").trim().toLowerCase();
                    const val = opt.value || "";
                    if (text === wanted || text.includes(wanted) || wanted.includes(text)) {
                        countryCode = val;
                        break;
                    }
                }
            }
            return {csrfName, csrfValue, countryCode};
        }
        """,
        {"country": args.country},
    )
    if not csrf["csrfName"] or not csrf["csrfValue"]:
        raise RuntimeError("Details page CSRF missing")

    payload = {
        "csrf_name": csrf["csrfName"],
        "csrf_value": csrf["csrfValue"],
        "firstName": args.first_name,
        "surname": args.last_name,
        "zipcode": args.zip,
        "country": csrf["countryCode"],
        "phoneNumber": args.phone,
        "phone-number": f"+44{args.phone}",
        "emailAddress": args.email,
        "emailAddressConfirm": args.email,
    }

    print(f"Submitting details for {args.first_name} {args.last_name}")
    await post_form(page, f"{BASE_URL}/en/reservationindividuelle/payment", payload)
    await page.wait_for_timeout(10000)



async def submit_donation_to_summary(page):
    print("Submitting donation=0 to summary using live form")

    result = await page.evaluate("""
    async () => {
        const form = document.forms[0];
        if (!form) return {ok:false, reason:"no form"};

        const csrfName = document.querySelector('input[name="csrf_name"]')?.value || "";
        const csrfValue = document.querySelector('input[name="csrf_value"]')?.value || "";
        if (!csrfName || !csrfValue) return {ok:false, reason:"missing csrf"};

        const check = document.querySelector('input[name="donation-check"]');
        if (check) check.checked = false;

        const hiddenCheck = document.querySelector('input[name="donationCheck"]');
        if (hiddenCheck) hiddenCheck.value = "true";

        const radios = Array.from(document.querySelectorAll('input[name="donation-input"][type="radio"]'));
        for (const r of radios) r.checked = false;

        const num = document.querySelector('input[name="donation-input"][type="number"]');
        if (num) num.value = "0";

        return {
            ok: true,
            action: form.action,
            csrfName,
            csrfValue,
            inputs: Array.from(form.querySelectorAll("input")).map(i => ({
                name: i.name,
                type: i.type,
                value: i.value,
                checked: i.checked
            }))
        };
    }
    """)

    print("Donation form prepared:")
    print(json.dumps(result, indent=2))

    if not result.get("ok"):
        raise RuntimeError(f"Donation form prepare failed: {result}")

    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=90000):
            await page.locator("form").evaluate("(form) => form.submit()")
    except Exception as exc:
        print(f"Donation navigation wait warning: {exc}")
        await page.wait_for_timeout(12000)

    await page.wait_for_timeout(5000)
    print("After donation submit URL:", page.url)


async def solve_turnstile_capsolver(page) -> str:
    """Solve Turnstile on current page via CapSolver. Returns token or empty string."""
    capsolver_api_key = os.getenv("CAPSOLVER_API_KEY", "").strip()
    if not capsolver_api_key:
        print("ERROR: CAPSOLVER_API_KEY not set")
        return ""

    page_url = page.url

    # Extract sitekey from DOM
    sitekey_data = await page.evaluate("""
    () => {
        const el = document.querySelector('.cf-turnstile, [data-sitekey]');
        if (el) return el.getAttribute('data-sitekey') || '';
        const iframe = document.querySelector('iframe[src*="turnstile"]');
        if (iframe) {
            const m = (iframe.src || '').match(/[?&]k=([^&]+)/);
            if (m) return m[1];
        }
        for (const s of document.querySelectorAll('script')) {
            const m = s.textContent.match(/sitekey["\\s:]+["']([0-9a-zA-Z_-]+)["']/);
            if (m) return m[1];
        }
        return '';
    }
    """)

    site_key = sitekey_data or DEFAULT_PAYMENT_TURNSTILE_SITEKEY
    if not sitekey_data:
        print(f"Turnstile sitekey not found in DOM; using fallback: {site_key[:8]}…")

    print(f"Solving Turnstile (sitekey: {site_key[:8]}…, url: {page_url})")

    create_payload = {
        "clientKey": capsolver_api_key,
        "task": {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": site_key,
        },
    }

    CAPSOLVER_CREATE_URL = "https://api.capsolver.com/createTask"
    CAPSOLVER_RESULT_URL = "https://api.capsolver.com/getTaskResult"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
        async with http.post(CAPSOLVER_CREATE_URL, json=create_payload) as resp:
            create_data = await resp.json(content_type=None)

        if create_data.get("errorId", 0) != 0:
            print(f"CapSolver createTask error: {create_data.get('errorDescription', '')}")
            return ""

        task_id = create_data.get("taskId", "")
        if not task_id:
            print("CapSolver returned no taskId")
            return ""

        print(f"CapSolver task created: {task_id}")

        for _ in range(90):
            await asyncio.sleep(2)
            async with http.post(
                CAPSOLVER_RESULT_URL,
                json={"clientKey": capsolver_api_key, "taskId": task_id},
            ) as resp:
                result_data = await resp.json(content_type=None)

            status = result_data.get("status", "")
            if status == "ready":
                token = result_data.get("solution", {}).get("token", "")
                print(f"Turnstile solved (token: {token[:20]}…)")
                return token
            elif status == "failed":
                print(f"CapSolver task failed: {result_data.get('errorDescription', '')}")
                return ""

    print("CapSolver polling timed out")
    return ""


async def complete_payment_auto(page):
    """Click T&C, solve Turnstile via CapSolver, inject token, click Complete, wait for /thank-you."""
    print("AUTO COMPLETE: starting automated payment completion")

    # 1. Check T&C checkbox
    tc_checked = await page.evaluate("""
    () => {
        const cb = document.querySelector('input[name="terms-and-conditions"]');
        if (!cb) return false;
        cb.checked = true;
        cb.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
    }
    """)
    if tc_checked:
        print("AUTO COMPLETE: T&C checkbox checked")
    else:
        print("AUTO COMPLETE: T&C checkbox not found in DOM, trying Playwright click")
        try:
            await page.locator('input[name="terms-and-conditions"]').check()
        except Exception as e:
            print(f"AUTO COMPLETE: T&C click warning: {e}")

    # 2. Solve Turnstile
    token = await solve_turnstile_capsolver(page)
    if not token:
        print("AUTO COMPLETE: WARNING — no Turnstile token, submission may fail")

    # 3. Inject token
    if token:
        injected = await page.evaluate(
            """(token) => {
                const inputs = document.querySelectorAll('input[name="cf-turnstile-response"]');
                inputs.forEach(i => i.value = token);
                const widgets = document.querySelectorAll('.cf-turnstile');
                widgets.forEach(w => {
                    let inp = w.querySelector('input[name="cf-turnstile-response"]');
                    if (!inp) {
                        inp = document.createElement('input');
                        inp.type = 'hidden';
                        inp.name = 'cf-turnstile-response';
                        w.appendChild(inp);
                    }
                    inp.value = token;
                });
                return inputs.length + widgets.length;
            }""",
            token,
        )
        print(f"AUTO COMPLETE: Injected token into {injected} element(s)")

    # 4. Click Complete button
    print("AUTO COMPLETE: clicking Complete button")
    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=90000):
            clicked = await page.evaluate("""
            () => {
                const btn = document.querySelector('button[type="submit"], input[type="submit"], button.complete, .btn-complete');
                if (btn) { btn.click(); return true; }
                const form = document.forms[0];
                if (form) { form.submit(); return true; }
                return false;
            }
            """)
            if not clicked:
                print("AUTO COMPLETE: submit element not found via JS, trying Playwright")
                await page.locator('button[type="submit"]').first.click()
    except Exception as exc:
        print(f"AUTO COMPLETE: navigation wait warning: {exc}")
        await page.wait_for_timeout(15000)

    await page.wait_for_timeout(5000)
    final_url = page.url
    print(f"AUTO COMPLETE: redirected to {final_url}")

    # 5. Save result
    after_html = await page.content()
    after_path = Path("/Users/amlendupandey/Downloads/ndame/after_auto_complete.html")
    after_path.write_text(after_html, encoding="utf-8")

    after_data = await page.evaluate("""
    () => ({
        url: location.href,
        title: document.title,
        text: document.body ? document.body.innerText.slice(0, 1500) : "",
        hasOrderHash: location.href.includes("orderHash"),
        hasThankYou: document.body ? document.body.innerText.toLowerCase().includes("thank") : false
    })
    """)
    print("AUTO COMPLETE RESULT:")
    print(json.dumps(after_data, indent=2))
    print("Saved HTML:", after_path)

    if after_data.get("hasThankYou") or after_data.get("hasOrderHash"):
        print("AUTO COMPLETE: SUCCESS — booking confirmed!")
    else:
        print("AUTO COMPLETE: WARNING — thank-you page not detected, check HTML")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--time", required=True)
    parser.add_argument("--ticket-count", type=int, required=True)
    parser.add_argument("--first-name", required=True)
    parser.add_argument("--last-name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--phone", required=True)
    parser.add_argument("--zip", required=True)
    parser.add_argument("--country", default="United States Of America")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--stop-at-payment", action="store_true", default=True)
    parser.add_argument("--continue-to-summary", action="store_true")
    parser.add_argument("--continue-to-final", action="store_true")
    parser.add_argument("--manual-final-wait", action="store_true")
    parser.add_argument("--auto-complete", action="store_true", help="Auto-click T&C, solve Turnstile via CapSolver, and click Complete")
    args = parser.parse_args()

    proxy_line, profile_dir = load_lane(args.lane)
    proxy = playwright_proxy_from_line(proxy_line)

    print(f"Booking lane: {args.lane}")
    print(f"Profile: {profile_dir}")
    # LOCAL_FORCE_TEST_PROXY_PATCH
    env_proxy_line = os.environ.get("TEST_PROXY", "").strip()
    if env_proxy_line:
        proxy_line = env_proxy_line
        parts = proxy_line.split(":", 3)
        if len(parts) == 4:
            host, port, username, password = parts
            proxy = {
                "server": f"http://{host}:{port}",
                "username": username,
                "password": password,
            }
            print(f"FORCED Playwright proxy from TEST_PROXY: {host}:{port}")
        else:
            print("WARNING: TEST_PROXY format invalid, expected host:port:user:pass")

    print(f"Proxy: {proxy_display(proxy_line)}")

    async with async_playwright() as p:
        context_kwargs = {
            "user_data_dir": profile_dir,
            "headless": not args.headed,
            "locale": "en-US",
            "no_viewport": True,
            "args": [
                "--window-size=1440,900",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        # LOCAL_PROXY_EXTENSION_PATCH
        proxy_ext = os.environ.get("NDAME_PROXY_EXTENSION_DIR", "").strip()
        proxy_server_arg = os.environ.get("NDAME_PROXY_SERVER_ARG", "").strip()

        if proxy_server_arg:
            context_kwargs["args"].append(f"--proxy-server={proxy_server_arg}")
            print(f"Chrome proxy-server arg enabled: {proxy_server_arg}")

        if proxy_ext:
            context_kwargs["args"].extend([
                f"--disable-extensions-except={proxy_ext}",
                f"--load-extension={proxy_ext}",
            ])
            print(f"Proxy auth extension enabled: {proxy_ext}")

        print(f"DEBUG proxy object: {proxy}")

        if proxy:
            context_kwargs["proxy"] = proxy
            print(f"Playwright proxy enabled: {proxy.get('server')}")
        else:
            print("WARNING: Playwright proxy is empty; browser will use local/home IP")

        context = await p.chromium.launch_persistent_context(**context_kwargs)
        page = await context.new_page()

        # LOCAL_BROWSER_IP_CHECK_PATCH
        try:
            await page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=30000)
            ip_text = (await page.text_content("body")) or ""
            print(f"BROWSER_IP_CHECK: {ip_text}")
            if "73.156.137.139" in ip_text:
                print("WARNING: Proxy is not active, continuing anyway for this one test run")
        except Exception as e:
            print(f"BROWSER_IP_CHECK_FAILED: {e}")
            raise


        try:
            print("Opening tickets page")
            await page.goto(RESERVATION_URL, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(10000)

            html = await page.content()
            if "csrf_name" not in html:
                await page.screenshot(path="booking_tickets_blocked.png", full_page=True)
                with open("booking_tickets_blocked.html", "w", encoding="utf-8") as fh:
                    fh.write(await page.content())

                print("Tickets page missing CSRF/form.")
                print("Browser is open. If blocked/DataDome page is visible, solve it manually or refresh until the real tickets form appears.")
                await asyncio.to_thread(input, "After tickets page/form is visible, press Enter here to continue... ")

                await page.wait_for_load_state("domcontentloaded")
                state = await page.evaluate("""
                () => ({
                    csrf_name: document.querySelector('input[name="csrf_name"]')?.value || "",
                    csrf_value: document.querySelector('input[name="csrf_value"]')?.value || "",
                    ticket_field_name: document.querySelector('select[name^="tickets["]')?.getAttribute("name") || ""
                })
                """)

                csrf_name = state.get("csrf_name", "")
                csrf_value = state.get("csrf_value", "")
                ticket_field_name = state.get("ticket_field_name", "")

                if not csrf_name or not csrf_value or not ticket_field_name:
                    raise RuntimeError("Tickets page did not expose CSRF/form")

            print("Tickets page OK")
            await submit_tickets(page, args.ticket_count)

            html = await page.content()
            if "ticketDate" not in html and "csrf_name" not in html:
                raise RuntimeError("Calendar page not reached")

            print("Calendar page OK")
            await submit_calendar(page, args.date, args.time, args.ticket_count)

            html = await page.content()
            if "firstName" not in html and "emailAddress" not in html:
                raise RuntimeError("Personal details page not reached")

            print("Personal details page OK")
            await submit_details(page, args)

            current_url = page.url
            print(f"After details submit URL: {current_url}")

            html = await page.content()
            if "/payment" in current_url or "terms-and-conditions" in html or "paymentCheck" in html:
                print("PAYMENT PAGE REACHED ✅")

                from pathlib import Path
                inspect_path = Path("/Users/amlendupandey/Downloads/ndame/payment_page_live_inspect.html")
                inspect_path.write_text(html, encoding="utf-8")

                form_data = await page.evaluate("""
                () => ({
                    url: location.href,
                    title: document.title,
                    hasCsrfName: !!document.querySelector('input[name="csrf_name"]'),
                    hasCsrfValue: !!document.querySelector('input[name="csrf_value"]'),
                    hasTerms: !!document.querySelector('[name="terms-and-conditions"]'),
                    hasPaymentCheck: !!document.querySelector('[name="paymentCheck"]'),
                    hasTurnstile: !!document.querySelector('.cf-turnstile, [data-sitekey], iframe[src*="turnstile"]'),
                    sitekeys: Array.from(document.querySelectorAll('[data-sitekey]')).map(x => x.getAttribute('data-sitekey')),
                    forms: Array.from(document.forms).map(f => ({
                        action: f.action,
                        method: f.method,
                        inputs: Array.from(f.querySelectorAll('input,select,textarea')).map(i => ({
                            name: i.name,
                            type: i.type,
                            value: i.type === 'password' ? '***' : i.value
                        }))
                    }))
                })
                """)
                print("PAYMENT INSPECT:")
                print(json.dumps(form_data, indent=2))
                print("Saved HTML:", inspect_path)

                if args.continue_to_summary:
                    await submit_donation_to_summary(page)
                    summary_url = page.url
                    summary_html = await page.content()
                    summary_path = Path("/Users/amlendupandey/Downloads/ndame/summary_page_live_inspect.html")
                    summary_path.write_text(summary_html, encoding="utf-8")

                    summary_data = await page.evaluate("""
                    () => ({
                        url: location.href,
                        title: document.title,
                        hasCsrfName: !!document.querySelector('input[name="csrf_name"]'),
                        hasCsrfValue: !!document.querySelector('input[name="csrf_value"]'),
                        hasTerms: !!document.querySelector('[name="terms-and-conditions"]'),
                        hasPaymentCheck: !!document.querySelector('[name="paymentCheck"]'),
                        hasTurnstile: !!document.querySelector('.cf-turnstile, [data-sitekey], iframe[src*="turnstile"]'),
                        sitekeys: Array.from(document.querySelectorAll('[data-sitekey]')).map(x => x.getAttribute('data-sitekey')),
                        forms: Array.from(document.forms).map(f => ({
                            action: f.action,
                            method: f.method,
                            inputs: Array.from(f.querySelectorAll('input,select,textarea,button')).map(i => ({
                                name: i.name,
                                type: i.type,
                                value: i.type === 'password' ? '***' : i.value,
                                text: i.textContent || ""
                            }))
                        }))
                    })
                    """)
                    print("SUMMARY INSPECT:")
                    print(json.dumps(summary_data, indent=2))
                    print("Saved HTML:", summary_path)

                    if args.continue_to_final:
                        print("FINAL SUBMIT ENABLED: submitting summary form to /payment")
                        try:
                            async with page.expect_navigation(wait_until="domcontentloaded", timeout=90000):
                                await page.locator("form").evaluate("(form) => form.submit()")
                        except Exception as exc:
                            print(f"Final submit navigation wait warning: {exc}")
                            await page.wait_for_timeout(15000)

                        await page.wait_for_timeout(10000)
                        final_url = page.url
                        final_html = await page.content()
                        final_path = Path("/Users/amlendupandey/Downloads/ndame/final_page_live_inspect.html")
                        final_path.write_text(final_html, encoding="utf-8")

                        final_data = await page.evaluate("""
                        () => ({
                            url: location.href,
                            title: document.title,
                            text: document.body ? document.body.innerText.slice(0, 1000) : "",
                            hasOrderHash: location.href.includes("orderHash"),
                            hasThankYou: document.body ? document.body.innerText.toLowerCase().includes("thank") : false,
                            forms: Array.from(document.forms).map(f => ({
                                action: f.action,
                                method: f.method,
                                inputs: Array.from(f.querySelectorAll('input,select,textarea,button')).map(i => ({
                                    name: i.name,
                                    type: i.type,
                                    value: i.type === 'password' ? '***' : i.value,
                                    text: i.textContent || ""
                                }))
                            }))
                        })
                        """)
                        print("FINAL INSPECT:")
                        print(json.dumps(final_data, indent=2))
                        print("Saved HTML:", final_path)

                        if args.auto_complete:
                            await complete_payment_auto(page)
                        elif args.manual_final_wait:
                            print("Browser is waiting on final payment page.")
                            print("Open VNC, tick Terms & Conditions, click Complete, then press Enter here...")
                            input()
                            await page.wait_for_timeout(10000)

                            after_html = await page.content()
                            after_path = Path("/Users/amlendupandey/Downloads/ndame/after_manual_final.html")
                            after_path.write_text(after_html, encoding="utf-8")

                            after_data = await page.evaluate("""
                            () => ({
                                url: location.href,
                                title: document.title,
                                text: document.body ? document.body.innerText.slice(0, 1500) : "",
                                hasOrderHash: location.href.includes("orderHash"),
                                hasThankYou: document.body ? document.body.innerText.toLowerCase().includes("thank") : false
                            })
                            """)
                            print("AFTER MANUAL FINAL:")
                            print(json.dumps(after_data, indent=2))
                            print("Saved HTML:", after_path)

                        return

                    print("Stopping at summary. Final payment/confirmation is not auto-submitted.")
                    return

                print("Stopping here. Payment/Turnstile is not auto-submitted in this smoke test.")
                return

            print("Reached unexpected page:")
            print(current_url)
            print((await page.content())[:500])

        finally:
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
