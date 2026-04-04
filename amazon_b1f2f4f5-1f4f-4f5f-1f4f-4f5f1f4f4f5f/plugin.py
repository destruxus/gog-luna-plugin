"""GOG Galaxy - Amazon Luna Plugin."""

import base64
import json
import logging
import re
import sys
import uuid
from urllib.parse import unquote

import aiohttp
from yarl import URL

from galaxy.api.consts import LicenseType, Platform, SubscriptionDiscovery
from galaxy.api.errors import AuthenticationRequired, InvalidCredentials
from galaxy.api.plugin import Plugin, create_and_run_plugin
from galaxy.api.types import (
    Authentication,
    Game,
    LicenseInfo,
    NextStep,
    Subscription,
    SubscriptionGame,
)

logger = logging.getLogger("luna-plugin")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

_LOGIN_URL = "https://luna.amazon.com/login"

_AUTH_PARAMS = {
    "window_title": "Sign in to Amazon Luna",
    "window_width": 1280,
    "window_height": 720,
    "start_uri": _LOGIN_URL,
    "end_uri_regex": r"https://luna\.amazon\.[a-z]+/(?!login).+",
}

_SESSION_COOKIES = ("session-id", "session-token", "x-main", "at-main")

_API_BASE = "https://proxy-prod.eu-west-1.tempo.digital.a2z.com"

_MARKETPLACE = "A2NODRKZP88ZB9"  # Sweden

# Maps subscriber_tier text (lowercase) from luna.amazon.se to
# (GOG subscription name, getPage pageUri for the full game list).
_SUBSCRIPTION_TIERS = {
    "luna standard": (
        "Luna Standard",
        "subscription/luna-standard"
        "?channel=luna-standard&quick_search=title_a_to_z",
    ),
    "luna premium": (
        "Luna Premium",
        "subscription/luna-premium/B085TRCCT6"
        "?quick_search=title_a_to_z"
        "&channel=amzn1.adg.product.065de039-f85c-40f0-9d69-33020370912c",
    ),
}

_CLIENT_CONTEXT = {
    "browserMetadata": {
        "browserClientRole": "browser",
        "browserType": "Chrome",
        "browserVersion": "120.0.0.0",
        "deviceModel": "unknown",
        "deviceType": "unknown",
        "osName": "Windows",
        "osVersion": "10",
        "refMarker": None,
        "referrer": None,
    },
    "dynamicFeatures": ["VCC_EDUCATION_SHOWN"],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_page_context_body(page_uri):
    """Build a getPage request body using pageContext (URI-based pages)."""
    return json.dumps(
        {
            "timeout": 10000,
            "featureScheme": "WEB_V1",
            "cacheKey": str(uuid.uuid4()),
            "clientContext": _CLIENT_CONTEXT,
            "inputContext": {"gamepadTypes": []},
            "pageContext": {
                "pageId": "default",
                "pageUri": page_uri,
            },
        },
        separators=(",", ":"),
    )


def _cookies_from_list(cookie_list):
    """Extract relevant Amazon session cookies from GOG's cookie list."""
    return {
        c["name"]: c["value"]
        for c in cookie_list
        if c.get("name") in _SESSION_COOKIES
    }


def _cookie_header(cookies):
    """Render a cookie dict as a Cookie header string."""
    return "; ".join("{}={}".format(k, v) for k, v in cookies.items())


def _extract_titles(page_data, label="page"):
    """Walk the page widget tree, return {game_id: title} for all tiles."""
    games = {}
    type_counts = {}

    def walk(widgets):
        for widget in widgets:
            wtype = widget.get("type", "UNKNOWN")
            type_counts[wtype] = type_counts.get(wtype, 0) + 1
            if wtype == "GAME_TILE_VERTICAL":
                raw = widget.get("presentationData", "{}")
                try:
                    pd = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                game_id = pd.get("gameId")
                title = pd.get("title")
                if game_id and title and game_id not in games:
                    games[game_id] = title
                elif game_id and not title:
                    logger.debug(
                        "[%s] tile has gameId=%s but no title", label, game_id
                    )
            if "widgets" in widget:
                walk(widget["widgets"])

    groups = page_data.get("pageMemberGroups", {})
    logger.info(
        "[%s] pageMemberGroups: %s",
        label,
        {k: len(v.get("widgets", [])) for k, v in groups.items()},
    )
    for group in groups.values():
        walk(group.get("widgets", []))

    logger.info("[%s] widget types seen: %s", label, type_counts)
    return games


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class LunaPlugin(Plugin):
    """Amazon Luna integration for GOG Galaxy."""

    def __init__(self, reader, writer, token):
        super().__init__(Platform.Amazon, "1.0.0", reader, writer, token)
        self._cookies = {}
        self._session = None
        # Cached after authentication
        self._user_id = None
        self._display_name = None
        self._tier_entry = None  # (sub_name, page_uri) or None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar()
            )
            if self._cookies:
                self._session.cookie_jar.update_cookies(
                    self._cookies, URL(_API_BASE)
                )
        return self._session

    def _build_headers(self):
        at_main = unquote(self._cookies.get("at-main", ""))
        session_id = self._cookies.get("session-id", "")
        return {
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": "https://luna.amazon.se",
            "Referer": "https://luna.amazon.se/",
            "Cookie": _cookie_header(self._cookies),
            "x-amz-access-token": at_main,
            "x-amz-marketplace-id": _MARKETPLACE,
            "x-amz-device-type": "browser",
            "x-amz-platform": "web",
            "x-amz-locale": "en_US",
            "x-amz-session-id": session_id,
            "x-amz-device-serial-number": session_id,
            "x-amz-country-of-residence": "SE",
            "x-amz-timezone": "Europe/Stockholm",
            "x-amz-client-version": "-",
        }

    async def _fetch_page_by_uri(self, page_uri):
        """Call getPage via pageContext URI and return parsed JSON, or None."""
        http = await self._get_session()
        async with http.post(
            _API_BASE + "/getPage",
            headers=self._build_headers(),
            data=_build_page_context_body(page_uri),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error(
                    "getPage(uri=%s) failed: %s | %s",
                    page_uri, resp.status, text[:300],
                )
                return None
            return await resp.json(content_type=None)

    async def shutdown(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Profile + subscription — fetched once, cached for the session
    # ------------------------------------------------------------------

    async def _load_user_info(self):
        """Fetch username from Amazon and subscription tier from Luna.
        Populates self._user_id, self._display_name, self._tier_entry."""
        self._user_id = self._cookies.get("session-id", "luna-user")

        # 1. Username from amazon.com
        try:
            http = await self._get_session()
            async with http.get(
                "https://www.amazon.com/",
                headers={
                    "User-Agent": _USER_AGENT,
                    "Cookie": _cookie_header(self._cookies),
                    "Accept-Language": "en-US,en;q=0.9",
                },
            ) as resp:
                html = await resp.text()
            match = re.search(r'data-test-id="profile_name">([^<]+)<', html)
            if match:
                self._display_name = match.group(1).strip()
                logger.info("Logged in as: %s", self._display_name)
            else:
                logger.warning("profile_name not found on Amazon homepage")
                self._display_name = self._user_id
        except Exception as exc:
            logger.warning("Could not fetch Amazon homepage: %s", exc)
            self._display_name = self._user_id

        # 2. Subscription tier from luna.amazon.se
        try:
            http = await self._get_session()
            async with http.get(
                "https://luna.amazon.se/",
                headers={
                    "User-Agent": _USER_AGENT,
                    "Cookie": _cookie_header(self._cookies),
                    "Accept-Language": "en-US,en;q=0.9",
                },
            ) as resp:
                luna_html = await resp.text()
            match = re.search(
                r'data-test-id="subscriber_tier">([^<]+)<', luna_html
            )
            if match:
                tier_raw = match.group(1).strip()
                logger.info("Luna subscriber_tier: %s", tier_raw)
                self._tier_entry = _SUBSCRIPTION_TIERS.get(tier_raw.lower())
                if self._tier_entry is None:
                    logger.warning(
                        "Unrecognised tier %r — defaulting to Luna Standard",
                        tier_raw,
                    )
                    self._tier_entry = _SUBSCRIPTION_TIERS["luna standard"]
            else:
                logger.info(
                    "subscriber_tier not found — user may not have Luna access"
                )
                self._tier_entry = None
        except Exception as exc:
            logger.warning("Could not fetch Luna homepage: %s", exc)
            self._tier_entry = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, stored_credentials=None):
        if stored_credentials:
            self._cookies = stored_credentials
            await self._load_user_info()
            return Authentication(self._user_id, self._display_name)
        return NextStep("web_session", _AUTH_PARAMS)

    async def pass_login_credentials(self, step, credentials, cookies):
        session = _cookies_from_list(cookies)
        if not session.get("session-token") and not session.get("at-main"):
            raise InvalidCredentials()
        self._cookies = session
        self.store_credentials(session)
        await self._load_user_info()
        return Authentication(self._user_id, self._display_name)

    # ------------------------------------------------------------------
    # Owned games — from /purchased
    # ------------------------------------------------------------------

    async def get_owned_games(self):
        if not self._cookies:
            raise AuthenticationRequired()
        data = await self._fetch_page_by_uri("purchased")
        if data is None:
            return []
        titles = _extract_titles(data, label="purchased")
        logger.info("Found %d owned games", len(titles))
        return [
            Game(
                game_id=gid,
                game_title=title,
                dlcs=None,
                license_info=LicenseInfo(LicenseType.SinglePurchase),
            )
            for gid, title in titles.items()
        ]

    # ------------------------------------------------------------------
    # Subscriptions — tier detected at login, cached for the session
    # ------------------------------------------------------------------

    async def get_subscriptions(self):
        if not self._cookies:
            raise AuthenticationRequired()
        if self._tier_entry is None:
            return []
        sub_name, _ = self._tier_entry
        return [
            Subscription(
                subscription_name=sub_name,
                owned=True,
                subscription_discovery=SubscriptionDiscovery.AUTOMATIC,
            )
        ]

    async def get_subscription_games(self, subscription_name, context):
        if not self._cookies:
            raise AuthenticationRequired()
        if self._tier_entry is None:
            yield []
            return
        _, page_uri = self._tier_entry
        data = await self._fetch_page_by_uri(page_uri)
        if data is None:
            yield []
            return
        titles = _extract_titles(data, label=subscription_name)
        logger.info(
            "Found %d subscription games for %s", len(titles), subscription_name
        )
        yield [
            SubscriptionGame(game_title=title, game_id=gid)
            for gid, title in titles.items()
        ]


def main():
    create_and_run_plugin(LunaPlugin, sys.argv)


if __name__ == "__main__":
    main()
