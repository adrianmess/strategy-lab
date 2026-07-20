#!/usr/bin/env python3
"""Isolation test: does Playwright+Chromium actually use the Decodo proxy?
Run:  python3 test_proxy.py
Prints the Playwright version, the launch args, and the browser's egress IP
so we can see EXACTLY where the proxy is (or isn't) applied.
"""
import asyncio, json, os, sys, urllib.request

def version():
    try:
        import importlib.metadata as m
        return m.version("playwright")
    except Exception as e:
        return f"unknown ({e})"

async def main():
    from playwright.async_api import async_playwright

    pc = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "proxy_config.json")))
    proxy = {"server": pc["server"]}
    if pc.get("username"): proxy["username"] = pc["username"]
    if pc.get("password"): proxy["password"] = pc["password"]

    try:
        direct = urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode().strip()
    except Exception:
        direct = "?"
    print(f"Playwright version : {version()}")
    print(f"Proxy server       : {proxy['server']}")
    print(f"Your REAL IP       : {direct}")
    print("-" * 60)

    udd = os.path.abspath("chrome_user_data/_proxytest")
    os.makedirs(udd, exist_ok=True)
    args = [
        "--no-first-run", "--no-default-browser-check", "--no-sandbox",
        f"--proxy-server={proxy['server']}",
        "--proxy-bypass-list=<-loopback>",
    ]
    print("Launch args        :", args)

    async with async_playwright() as p:
        # A) persistent context WITH explicit --proxy-server arg (what the server does)
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=udd, headless=True, proxy=proxy, args=args)
        pg = await ctx.new_page()
        await pg.goto("https://api.ipify.org?format=text", timeout=25000)
        ipA = (await pg.inner_text("body")).strip()
        await ctx.close()
        print(f"[A] persistent + --proxy-server : {ipA}   "
              f"{'LEAK (real IP)' if ipA==direct else 'OK via proxy'}")

        # B) plain launch() with proxy= only (no persistent profile, no extra args)
        br = await p.chromium.launch(headless=True, proxy=proxy)
        pg = await br.new_page()
        await pg.goto("https://api.ipify.org?format=text", timeout=25000)
        ipB = (await pg.inner_text("body")).strip()
        await br.close()
        print(f"[B] launch + proxy= only        : {ipB}   "
              f"{'LEAK (real IP)' if ipB==direct else 'OK via proxy'}")

    print("-" * 60)
    print("If A leaks but B works -> the persistent-context/args path is the problem.")
    print("If both leak -> Playwright's Chromium isn't honoring this proxy at all.")

if __name__ == "__main__":
    asyncio.run(main())
