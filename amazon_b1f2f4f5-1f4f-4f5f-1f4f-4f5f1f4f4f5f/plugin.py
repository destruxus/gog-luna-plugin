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
    GameLibrarySettings,
    GameTime,
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

_AUTH_PARAMS = {
    "window_title": "Sign in to Amazon Luna",
    "window_width": 1280,
    "window_height": 720,
    "start_uri": "https://luna.amazon.com/login",
    "end_uri_regex": r"https://luna\.amazon\.[a-z]+/(?!login).+",
}

_SESSION_COOKIES = ("session-id", "session-token", "x-main", "at-main")

_API_BASE = "https://proxy-prod.eu-west-1.tempo.digital.a2z.com"

_MARKETPLACE = "A2NODRKZP88ZB9"  # Sweden

_LUNA_BASE = "https://luna.amazon.se"

# Maps subscriber_tier (lowercase) found on luna.amazon.se to
# (GOG subscription name, getPage pageUri).
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
# Request helpers
# ---------------------------------------------------------------------------


def _build_body(page_uri):
    """Build a getPage POST body using pageContext."""
    return json.dumps(
        {
            "timeout": 10000,
            "featureScheme": "WEB_V1",
            "cacheKey": str(uuid.uuid4()),
            "clientContext": _CLIENT_CONTEXT,
            "inputContext": {"gamepadTypes": []},
            "pageContext": {"pageId": "default", "pageUri": page_uri},
        },
        separators=(",", ":"),
    )


def _cookies_from_list(cookie_list):
    """Extract session cookies from GOG's cookie list format."""
    return {
        c["name"]: c["value"]
        for c in cookie_list
        if c.get("name") in _SESSION_COOKIES
    }


def _cookie_header(cookies):
    return "; ".join("{}={}".format(k, v) for k, v in cookies.items())


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------


def _extract_tiles(page_data, label="page"):
    """Walk the widget tree, return {game_id: presentationData} for tiles."""
    tiles = {}
    type_counts = {}

    def walk(widgets):
        for widget in widgets:
            wtype = widget.get("type", "UNKNOWN")
            type_counts[wtype] = type_counts.get(wtype, 0) + 1
            if wtype in ("GAME_TILE_VERTICAL", "GAME_TILE"):
                try:
                    pd = json.loads(widget.get("presentationData", "{}"))
                except (ValueError, TypeError):
                    continue
                game_id = pd.get("gameId")
                if game_id and game_id not in tiles:
                    tiles[game_id] = pd
                elif game_id and not pd.get("title"):
                    logger.debug("[%s] tile %s has no title", label, game_id)
            if "widgets" in widget:
                walk(widget["widgets"])

    groups = page_data.get("pageMemberGroups", {})
    logger.info(
        "[%s] groups: %s",
        label,
        {k: len(v.get("widgets", [])) for k, v in groups.items()},
    )
    for group in groups.values():
        walk(group.get("widgets", []))
    logger.info("[%s] widget types: %s", label, type_counts)
    return tiles


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class LunaPlugin(Plugin):
    """Amazon Luna integration for GOG Galaxy."""

    def __init__(self, reader, writer, token):
        super().__init__(Platform.Amazon, "1.0.0", reader, writer, token)
        self._cookies = {}
        self._session = None
        self._user_id = None
        self._display_name = None
        self._tier_entry = None  # (sub_name, page_uri) or None

    # ------------------------------------------------------------------
    # HTTP
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

    def _api_headers(self):
        at_main = unquote(self._cookies.get("at-main", ""))
        session_id = self._cookies.get("session-id", "")
        return {
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": _LUNA_BASE,
            "Referer": _LUNA_BASE + "/",
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

    def _browse_headers(self):
        return {
            "User-Agent": _USER_AGENT,
            "Cookie": _cookie_header(self._cookies),
            "Accept-Language": "en-US,en;q=0.9",
        }

    async def _get_page(self, page_uri):
        """POST to /getPage and return parsed JSON, or None on error."""
        http = await self._get_session()
        async with http.post(
            _API_BASE + "/getPage",
            headers=self._api_headers(),
            data=_build_body(page_uri),
        ) as resp:
            if resp.status != 200:
                logger.error(
                    "getPage(%s) → %s | %s",
                    page_uri, resp.status, (await resp.text())[:300],
                )
                return None
            return await resp.json(content_type=None)

    async def _get_html(self, url):
        """GET a page and return its HTML text, or None on error."""
        try:
            http = await self._get_session()
            async with http.get(url, headers=self._browse_headers()) as resp:
                return await resp.text()
        except Exception as exc:
            logger.warning("GET %s failed: %s", url, exc)
            return None

    async def shutdown(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Profile + subscription — loaded once at login
    # ------------------------------------------------------------------

    def _name_from_xmain(self):
        """Decode x-main cookie — returns customer name or None."""
        raw = self._cookies.get("x-main", "")
        if not raw:
            return None
        try:
            # x-main is a base64-encoded JSON blob; pad as needed.
            padded = raw + "=" * (-len(raw) % 4)
            data = json.loads(base64.b64decode(padded).decode("utf-8"))
            name = data.get("customerName") or data.get("name")
            if name:
                logger.info("Username from x-main: %s", name)
            else:
                logger.debug("x-main keys: %s", list(data.keys()))
            return name or None
        except Exception as exc:
            logger.debug("x-main decode failed: %s", exc)
            return None

    async def _load_user_info(self):
        """Populate username and subscription tier from Luna."""
        self._user_id = self._cookies.get("session-id", "luna-user")

        # 1. Try to decode username from x-main cookie (Luna/Amazon token).
        self._display_name = self._name_from_xmain()

        # 2. Fetch luna.amazon.se — subscription tier + username fallback.
        html = await self._get_html(_LUNA_BASE + "/")
        if html:
            if not self._display_name:
                m = re.search(
                    r'data-test-id="profile_name">([^<]+)<', html
                )
                if m:
                    self._display_name = m.group(1).strip()
                    logger.info("Logged in as: %s", self._display_name)
                else:
                    logger.warning(
                        "profile_name not found on luna.amazon.se"
                    )

            m = re.search(
                r'data-test-id="subscriber_tier">([^<]+)<', html
            )
            if m:
                tier_raw = m.group(1).strip()
                logger.info("Luna subscriber_tier: %s", tier_raw)
                self._tier_entry = _SUBSCRIPTION_TIERS.get(tier_raw.lower())
                if self._tier_entry is None:
                    logger.warning(
                        "Unknown tier %r, defaulting to Luna Standard",
                        tier_raw,
                    )
                    self._tier_entry = _SUBSCRIPTION_TIERS["luna standard"]
            else:
                logger.info(
                    "No subscriber_tier found — no active subscription"
                )

        self._display_name = self._display_name or self._user_id

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
    # Owned games — luna.amazon.se/purchased
    # ------------------------------------------------------------------

    async def get_owned_games(self):
        if not self._cookies:
            raise AuthenticationRequired()
        data = await self._get_page("purchased")
        if data is None:
            return []
        tiles = _extract_tiles(data, label="purchased")
        logger.info("Found %d owned games", len(tiles))
        return [
            Game(
                game_id=gid,
                game_title=pd.get("title", gid),
                dlcs=None,
                license_info=LicenseInfo(LicenseType.SinglePurchase),
            )
            for gid, pd in tiles.items()
        ]

    # ------------------------------------------------------------------
    # Subscription games — luna.amazon.se/subscription/luna-*
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
        data = await self._get_page(page_uri)
        if data is None:
            yield []
            return
        tiles = _extract_tiles(data, label=subscription_name)
        logger.info("Found %d games for %s", len(tiles), subscription_name)
        yield [
            SubscriptionGame(game_title=pd.get("title", gid), game_id=gid)
            for gid, pd in tiles.items()
        ]

    # ------------------------------------------------------------------
    # Launch — opens luna.amazon.se/play/{game_id} in the browser
    # ------------------------------------------------------------------

    async def launch_game(self, game_id):
        await self.open_uri("{}/play/{}".format(_LUNA_BASE, game_id))

    # ------------------------------------------------------------------
    # Game time — per-game playtime from luna.amazon.se/game/{id}
    # ------------------------------------------------------------------

    async def get_game_time(self, game_id, context):
        if not self._cookies:
            raise AuthenticationRequired()
        # context is the {game_id: pd} dict built in prepare_game_times_context
        pd = (context or {}).get(game_id, {})
        time_played = pd.get("minutesPlayed")
        last_played = pd.get("lastPlayedTime")  # unix timestamp or None
        logger.info(
            "GameTime %s: played=%s min, last=%s",
            game_id, time_played, last_played,
        )
        return GameTime(
            game_id=game_id,
            time_played=int(time_played) if time_played is not None else None,
            last_played_time=(
                int(last_played) if last_played is not None else None
            ),
        )

    async def prepare_game_times_context(self, game_ids):
        """Fetch the game detail page for each owned game once and cache the
        presentationData so get_game_time() doesn't need an extra HTTP call."""
        if not self._cookies:
            return {}
        context = {}
        for gid in game_ids:
            data = await self._get_page("game/{}".format(gid))
            if data is None:
                continue
            groups = data.get("pageMemberGroups", {})
            type_counts = {}
            for group in groups.values():
                for widget in group.get("widgets", []):
                    wtype = widget.get("type", "UNKNOWN")
                    type_counts[wtype] = type_counts.get(wtype, 0) + 1
                    raw = widget.get("presentationData")
                    if raw:
                        try:
                            pd = json.loads(raw)
                            if pd.get("gameId") == gid:
                                context[gid] = pd
                                break
                        except (ValueError, TypeError):
                            pass
            logger.info(
                "[game/%s] widget types: %s", gid, type_counts
            )
        return context

    # ------------------------------------------------------------------
    # Tags / metadata — genres from presentationData
    # ------------------------------------------------------------------

    async def get_game_library_settings(self, game_id, context):
        if not self._cookies:
            raise AuthenticationRequired()
        # context is the same {game_id: pd} dict from prepare context
        pd = (context or {}).get(game_id, {})
        genres = pd.get("genres") or pd.get("tags") or []
        tags = [str(g) for g in genres] if genres else None
        return GameLibrarySettings(game_id=game_id, tags=tags, hidden=None)

    async def prepare_game_library_settings_context(self, game_ids):
        """Reuse game detail pages already fetched; avoids duplicate calls
        when game time and tags are imported in the same session."""
        return await self.prepare_game_times_context(game_ids)

    # ------------------------------------------------------------------
    # Achievements — luna.amazon.se/game/{game_id}/achievements
    # ------------------------------------------------------------------

    async def get_unlocked_achievements(self, game_id, context):
        if not self._cookies:
            raise AuthenticationRequired()
        data = await self._get_page(
            "game/{}/achievements".format(game_id)
        )
        if data is None:
            return []
        groups = data.get("pageMemberGroups", {})
        logger.info(
            "[achievements/%s] groups: %s",
            game_id,
            {k: len(v.get("widgets", [])) for k, v in groups.items()},
        )
        type_counts = {}

        def walk(widgets):
            for widget in widgets:
                wtype = widget.get("type", "UNKNOWN")
                type_counts[wtype] = type_counts.get(wtype, 0) + 1
                raw = widget.get("presentationData")
                if raw:
                    try:
                        pd = json.loads(raw)
                        logger.debug(
                            "[achievements/%s] %s keys: %s",
                            game_id, wtype, list(pd.keys()),
                        )
                    except (ValueError, TypeError):
                        pass
                if "widgets" in widget:
                    walk(widget["widgets"])

        for group in groups.values():
            walk(group.get("widgets", []))
        logger.info(
            "[achievements/%s] widget types: %s", game_id, type_counts
        )
        return []


def main():
    create_and_run_plugin(LunaPlugin, sys.argv)


if __name__ == "__main__":
    main()
