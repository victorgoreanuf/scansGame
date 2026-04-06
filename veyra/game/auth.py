"""Login flow — ported from slasher_app.py:86-114."""

import re

from bs4 import BeautifulSoup
import httpx

from veyra.game.endpoints import BASE_URL, HEADERS, LOGIN_URL, WAVE_MAP


async def do_login(client: httpx.AsyncClient, email: str, password: str) -> bool:
    """Authenticate against the game. Returns True on success."""
    page = await client.get(LOGIN_URL, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(page.text, "html.parser")

    form = soup.select_one("form")
    payload: dict[str, str] = {}
    if form:
        for inp in form.select("input"):
            name = inp.get("name")
            if name:
                payload[name] = inp.get("value", "")

    # Fill in credentials by matching field names
    for key in list(payload):
        kl = key.lower()
        if "email" in kl or "user" in kl or "login" in kl:
            payload[key] = email
        if "pass" in kl:
            payload[key] = password

    # Ensure email/password fields exist even if form parsing missed them
    if not any("email" in k.lower() or "user" in k.lower() for k in payload):
        payload["email"] = email
    if not any("pass" in k.lower() for k in payload):
        payload["password"] = password

    # Resolve form action URL
    action = LOGIN_URL
    if form and form.get("action"):
        a = form["action"]
        action = a if a.startswith("http") else f"{BASE_URL}/{a.lstrip('/')}"

    resp = await client.post(action, data=payload, headers=HEADERS, timeout=15)

    # Check for successful login indicators
    if "logout" in resp.text.lower() or "sign out" in resp.text.lower():
        return True
    if str(resp.url) != LOGIN_URL:
        return True

    # Fallback: try fetching a wave page and check for monster cards
    test = await client.get(list(WAVE_MAP.values())[0], headers=HEADERS, timeout=15)
    return "monster-card" in test.text


def extract_user_id(soup: BeautifulSoup) -> str:
    """Extract user ID from a page. Falls back to '150205'."""
    link = soup.select_one('a[href*="player.php?pid="]')
    if link:
        m = re.search(r"pid=(\d+)", link["href"])
        if m:
            return m.group(1)
    inp = soup.select_one('input[name="user_id"]')
    if inp:
        return inp.get("value", "150205")
    return "150205"
