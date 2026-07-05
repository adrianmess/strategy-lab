from playwright.sync_api import sync_playwright, TimeoutError
import time
import os

def main():
    with sync_playwright() as p:
        # Path to the uBlock Origin extension
        ublock_path = os.path.abspath("ublock-origin-built")

        # Check if the extension directory exists
        if not os.path.exists(ublock_path):
            print(f"Error: Extension directory not found at {ublock_path}")
            print("Please make sure the uBlock Origin extension is built and extracted to the 'ublock-origin-built' directory")
            return

        # Check if manifest.json exists in the extension directory
        manifest_path = os.path.join(ublock_path, "manifest.json")
        if not os.path.exists(manifest_path):
            print(f"Error: manifest.json not found at {manifest_path}")
            print("The extension directory must contain a manifest.json file.")
            return

        print(f"Loading uBlock Origin extension from {ublock_path}")

        # Launch browser with the uBlock Origin extension
        browser = p.chromium.launch(
            headless=False,  # Make browser visible for debugging
            args=[f'--disable-extensions-except={ublock_path}', f'--load-extension={ublock_path}']
        )

        # Create a new context with custom user agent
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"
        )

        # Create a new page
        page = context.new_page()

        # Navigate to MEXC
        try:
            print("Navigating to MEXC...")
            page.goto("https://www.mexc.com/futures/SOL_USDT?type=linear_swap", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)

            # Check if user is logged in by looking for the "Wallets" element
            try:
                wallets_element = page.get_by_role("link", name="Wallets")
                if wallets_element.is_visible():
                    print("User is logged in (Wallets link found).")
                else:
                    print("User is not logged in (Wallets link not found).")
            except Exception as e:
                print(f"Error checking login status: {e}")
                print("User is not logged in (could not find Wallets link).")

            # Take a screenshot for verification
            page.screenshot(path="mexc_homepage.png")

            # Print the title to verify we're on the right page
            print(f"Page title: {page.title()}")
        except TimeoutError:
            print("Timeout when accessing MEXC.")
            # Take a screenshot anyway to see what's happening
            page.screenshot(path="mexc_timeout.png")
        except Exception as e:
            print(f"Error accessing MEXC: {e}")

        # Wait for user to see the results
        time.sleep(30)

        # Close the browser
        browser.close()

if __name__ == "__main__":
    main()