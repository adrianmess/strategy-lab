#!/usr/bin/env python3
import asyncio
from playwright.async_api import async_playwright
import os
import time
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def debug_selectors():
    """Debug Playwright selectors by testing them on the MEXC website."""
    async with async_playwright() as p:
        # Launch the browser
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        # Navigate to MEXC
        logger.info("Navigating to MEXC...")
        await page.goto("https://www.mexc.com/futures/SOL_USDT?type=linear_swap", timeout=60000)
        await page.wait_for_load_state("networkidle", timeout=60000)

        # Take a screenshot of the initial page
        await page.screenshot(path="debug_initial.png")
        logger.info("Initial screenshot saved to debug_initial.png")

        # Wait for user to log in
        logger.info("Please log in to your MEXC account now...")
        logger.info("After logging in, navigate to the trading page and press Enter in this terminal to continue...")

        # Wait for user input
        input("Press Enter after you have logged in and are on the trading page...")

        # Take a screenshot after login
        await page.screenshot(path="debug_after_login.png")
        logger.info("Post-login screenshot saved to debug_after_login.png")

        # Test different selectors for the open tab
        logger.info("Testing open tab selectors...")

        # Test selector 1: test-id
        try:
            open_tab = page.get_by_test_id("contract-trade-order-form-tab-open")
            is_visible = await open_tab.is_visible()
            logger.info(f"Selector 1 (test-id): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await open_tab.click()
                await page.screenshot(path="debug_after_open_tab_click.png")
                logger.info("Clicked open tab, screenshot saved to debug_after_open_tab_click.png")
        except Exception as e:
            logger.error(f"Error with selector 1: {str(e)}")

        # Test selector 2: CSS selector
        try:
            open_tab = page.locator("div[data-testid='contract-trade-order-form-tab-open']")
            is_visible = await open_tab.is_visible()
            logger.info(f"Selector 2 (CSS): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await open_tab.click()
                await page.screenshot(path="debug_after_open_tab_click_css.png")
                logger.info("Clicked open tab with CSS selector, screenshot saved")
        except Exception as e:
            logger.error(f"Error with selector 2: {str(e)}")

        # Test selector 3: XPath
        try:
            open_tab = page.locator("//div[@data-testid='contract-trade-order-form-tab-open']")
            is_visible = await open_tab.is_visible()
            logger.info(f"Selector 3 (XPath): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await open_tab.click()
                await page.screenshot(path="debug_after_open_tab_click_xpath.png")
                logger.info("Clicked open tab with XPath selector, screenshot saved")
        except Exception as e:
            logger.error(f"Error with selector 3: {str(e)}")

        # Test selector 4: Text content
        try:
            open_tab = page.get_by_text("Open", exact=False)
            is_visible = await open_tab.is_visible()
            logger.info(f"Selector 4 (Text): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await open_tab.click()
                await page.screenshot(path="debug_after_open_tab_click_text.png")
                logger.info("Clicked open tab with text selector, screenshot saved")
        except Exception as e:
            logger.error(f"Error with selector 4: {str(e)}")

        # Test selector 5: Role
        try:
            open_tab = page.get_by_role("tab", name="Open", exact=False)
            is_visible = await open_tab.is_visible()
            logger.info(f"Selector 5 (Role): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await open_tab.click()
                await page.screenshot(path="debug_after_open_tab_click_role.png")
                logger.info("Clicked open tab with role selector, screenshot saved")
        except Exception as e:
            logger.error(f"Error with selector 5: {str(e)}")

        # Wait for user to confirm before continuing
        input("Press Enter to continue testing market tab selectors...")

        # Test market tab selectors
        logger.info("Testing market tab selectors...")

        # Test selector 1: CSS + Text
        try:
            market_tab = page.locator("#mexc_contract_v_open_position").get_by_text("Market")
            is_visible = await market_tab.is_visible()
            logger.info(f"Market selector 1 (CSS+Text): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await market_tab.click()
                await page.screenshot(path="debug_after_market_tab_click.png")
                logger.info("Clicked market tab, screenshot saved")
        except Exception as e:
            logger.error(f"Error with market selector 1: {str(e)}")

        # Test selector 2: Direct CSS
        try:
            market_tab = page.locator("div.market-tab")
            is_visible = await market_tab.is_visible()
            logger.info(f"Market selector 2 (Direct CSS): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await market_tab.click()
                await page.screenshot(path="debug_after_market_tab_click_css.png")
                logger.info("Clicked market tab with CSS selector, screenshot saved")
        except Exception as e:
            logger.error(f"Error with market selector 2: {str(e)}")

        # Wait for user to confirm before continuing
        input("Press Enter to continue testing quantity slider selectors...")

        # Test quantity slider selectors
        logger.info("Testing quantity slider selectors...")

        # Test selector 1: Original selector
        try:
            quantity_slider = page.locator(".ant-slider-step > span:nth-child(5)").first
            is_visible = await quantity_slider.is_visible()
            logger.info(f"Quantity slider selector 1 (Original): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await quantity_slider.click()
                await page.screenshot(path="debug_after_quantity_slider_click.png")
                logger.info("Clicked quantity slider, screenshot saved")
        except Exception as e:
            logger.error(f"Error with quantity slider selector 1: {str(e)}")

        # Test selector 2: CSS with nth-of-type
        try:
            quantity_slider = page.locator(".ant-slider-step > span:nth-of-type(5)")
            is_visible = await quantity_slider.is_visible()
            logger.info(f"Quantity slider selector 2 (nth-of-type): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await quantity_slider.click()
                await page.screenshot(path="debug_after_quantity_slider_click_nth.png")
                logger.info("Clicked quantity slider with nth-of-type selector, screenshot saved")
        except Exception as e:
            logger.error(f"Error with quantity slider selector 2: {str(e)}")

        # Test selector 3: Direct CSS
        try:
            quantity_slider = page.locator(".ant-slider-step span")
            is_visible = await quantity_slider.is_visible()
            logger.info(f"Quantity slider selector 3 (Direct CSS): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                # Try to click the 5th element
                elements = await quantity_slider.all()
                if len(elements) >= 5:
                    await elements[4].click()  # 0-indexed, so 4 is the 5th element
                    await page.screenshot(path="debug_after_quantity_slider_click_direct.png")
                    logger.info("Clicked quantity slider with direct CSS selector, screenshot saved")
                else:
                    logger.error(f"Not enough slider elements found. Found {len(elements)} elements.")
        except Exception as e:
            logger.error(f"Error with quantity slider selector 3: {str(e)}")

        # Test selector 4: XPath
        try:
            quantity_slider = page.locator("//div[contains(@class, 'ant-slider-step')]/span[5]")
            is_visible = await quantity_slider.is_visible()
            logger.info(f"Quantity slider selector 4 (XPath): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await quantity_slider.click()
                await page.screenshot(path="debug_after_quantity_slider_click_xpath.png")
                logger.info("Clicked quantity slider with XPath selector, screenshot saved")
        except Exception as e:
            logger.error(f"Error with quantity slider selector 4: {str(e)}")

        # Wait for user to confirm before continuing
        input("Press Enter to continue testing open long button selectors...")

        # Test open long button selectors
        logger.info("Testing open long button selectors...")

        # Test selector 1: test-id
        try:
            open_long_btn = page.get_by_test_id("contract-trade-open-long-btn")
            is_visible = await open_long_btn.is_visible()
            logger.info(f"Open long button selector 1 (test-id): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await open_long_btn.click()
                await page.screenshot(path="debug_after_open_long_click.png")
                logger.info("Clicked open long button, screenshot saved")
        except Exception as e:
            logger.error(f"Error with open long button selector 1: {str(e)}")

        # Test selector 2: CSS
        try:
            open_long_btn = page.locator("button[data-testid='contract-trade-open-long-btn']")
            is_visible = await open_long_btn.is_visible()
            logger.info(f"Open long button selector 2 (CSS): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await open_long_btn.click()
                await page.screenshot(path="debug_after_open_long_click_css.png")
                logger.info("Clicked open long button with CSS selector, screenshot saved")
        except Exception as e:
            logger.error(f"Error with open long button selector 2: {str(e)}")

        # Test selector 3: Text
        try:
            open_long_btn = page.get_by_text("Open Long", exact=False)
            is_visible = await open_long_btn.is_visible()
            logger.info(f"Open long button selector 3 (Text): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await open_long_btn.click()
                await page.screenshot(path="debug_after_open_long_click_text.png")
                logger.info("Clicked open long button with text selector, screenshot saved")
        except Exception as e:
            logger.error(f"Error with open long button selector 3: {str(e)}")

        # Test selector 4: Role
        try:
            open_long_btn = page.get_by_role("button", name="Open Long", exact=False)
            is_visible = await open_long_btn.is_visible()
            logger.info(f"Open long button selector 4 (Role): {'Visible' if is_visible else 'Not visible'}")
            if is_visible:
                await open_long_btn.click()
                await page.screenshot(path="debug_after_open_long_click_role.png")
                logger.info("Clicked open long button with role selector, screenshot saved")
        except Exception as e:
            logger.error(f"Error with open long button selector 4: {str(e)}")

        # Wait for user to see the results
        logger.info("Debugging complete. Check the screenshots and logs for results.")
        input("Press Enter to close the browser...")

        # Close the browser
        await browser.close()

if __name__ == "__main__":
    asyncio.run(debug_selectors())