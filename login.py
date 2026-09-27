"""One-time manual login to Wallapop.

Run this once (``python login.py``). It opens a real Chromium window pointed at
Wallapop. You log in by hand (email, Google, phone code, whatever you use), then
press Enter in the terminal. The browser session (cookies) is saved to
storage_state.json so the access layer can reuse it without logging in again.

Re-run it whenever the saved session expires and requests start getting blocked.
"""

from playwright.sync_api import sync_playwright

from access import STORAGE_STATE_PATH

LOGIN_URL = "https://es.wallapop.com/"


def main():
    """Open a browser, wait for manual login, then save the session to disk."""
    with sync_playwright() as p:
        # headless=False so you can see the page and log in yourself.
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(LOGIN_URL)

        print("\n" + "=" * 60)
        print("A browser window has opened.")
        print("1. Log in to Wallapop with your account.")
        print("2. Once you see your logged-in home page, come back here.")
        print("3. Press Enter to save the session.")
        print("=" * 60 + "\n")
        input("Press Enter when you are logged in... ")

        context.storage_state(path=STORAGE_STATE_PATH)
        browser.close()
        print(f"\nSession saved to {STORAGE_STATE_PATH}")


if __name__ == "__main__":
    main()
