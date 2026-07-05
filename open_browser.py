from playwright.sync_api import sync_playwright, TimeoutError
import os
import time

def main():
    with sync_playwright() as p:
        # Path to the uBlock Origin extension
        # You need to download the built extension from the Chrome Web Store
        # or build it yourself using the instructions in the uBlock-master repository
        # Then extract it to a directory and update this path
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

        # Use a persistent directory for Chrome user data
        user_data_dir = os.path.abspath("chrome_user_data")
        if not os.path.exists(user_data_dir):
            os.makedirs(user_data_dir)

        print(f"Using Chrome user data directory: {user_data_dir}")

        # Launch browser with persistent context and the uBlock Origin extension
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,  # Make browser visible
            args=[
                f'--disable-extensions-except={ublock_path}',
                f'--load-extension={ublock_path}',
                '--no-first-run',
                '--no-default-browser-check'
            ],
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"
        )

        # Create a new page
        page = context.new_page()

        # Navigate to MEXC
        try:
            print("Navigating to MEXC...")
            page.goto("https://www.mexc.com/futures/SOL_USDT?type=linear_swap", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)

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

        # Keep the browser open until manually closed
        input("Press Enter to close the browser...")

        # Close the context
        context.close()

if __name__ == "__main__":
    main()