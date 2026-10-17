"""Catalog lookups: JustWatch (where to watch + per-episode links) and TVmaze (air dates)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

_LOGGER = logging.getLogger(__name__)

JW_URL = "https://apis.justwatch.com/graphql"
TVMAZE_URL = "https://api.tvmaze.com"
TIMEOUT = aiohttp.ClientTimeout(total=25)

# Tracking parameters JustWatch adds to links; dropping them keeps links clean.
_DROP_PARAMS = {"at", "ct", "itscg", "itsct", "utm_source", "utm_medium", "utm_campaign"}

_SEARCH_QUERY = """
query Search($country: Country!, $searchCountry: Country!, $language: Language!, $filter: TitleFilter) {
  popularTitles(country: $searchCountry, first: 8, filter: $filter) {
    edges { node {
      id objectType
      content(country: $country, language: $language) {
        title originalReleaseYear externalIds { imdbId } posterUrl
      }
      offers(country: $country, platform: WEB) {
        monetizationType package { technicalName clearName }
      }
    } }
  }
}
"""

_SHOW_QUERY = """
query Show($id: ID!, $country: Country!, $language: Language!) {
  node(id: $id) {
    ... on Show {
      id
      content(country: $country, language: $language) {
        title externalIds { imdbId } posterUrl
      }
      offers(country: $country, platform: WEB) {
        monetizationType standardWebURL package { technicalName clearName }
      }
      seasons {
        content(country: $country, language: $language) { seasonNumber }
        episodes {
          content(country: $country, language: $language) { episodeNumber title }
          offers(country: $country, platform: WEB) {
            monetizationType standardWebURL package { technicalName }
          }
        }
      }
    }
  }
}
"""

_PAID = {"FLATRATE", "FREE", "ADS", "FLATRATE_AND_BUY"}

# JustWatch sometimes leaves a new show out of a country's search for weeks while
# already listing its offers there (Alley Cats in NL). A second search in a big
# catalog finds it; titles and offers still come from the user's country.
_FALLBACK_SEARCH = ("US", "GB")


class CatalogError(Exception):
    """A catalog lookup failed."""


def poster_url(path: str | None) -> str | None:
    """JustWatch poster path template -> image URL."""
    if not path:
        return None
    return "https://images.justwatch.com" + path.replace("{profile}", "s332").replace(
        "{format}", "jpg"
    )


def clean_url(url: str) -> str:
    """Strip affiliate/tracking parameters from a link."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in _DROP_PARAMS]
    return urlunsplit(parts._replace(query=urlencode(query)))


@dataclass
class SearchResult:
    """A show found by a search."""

    jw_id: str
    title: str
    year: int | None
    imdb_id: str | None
    services: dict[str, str]  # technical name -> display name
    poster: str | None = None

    @property
    def label(self) -> str:
        year = f" ({self.year})" if self.year else ""
        services = ", ".join(sorted(self.services.values())) or "not streaming in your country"
        return f"{self.title}{year} — {services}"


@dataclass
class Episode:
    """One episode, with what we know about where/when to watch it."""

    season: int
    episode: int
    title: str | None = None
    links: dict[str, str] = field(default_factory=dict)  # service -> url
    airstamp: datetime | None = None
    runtime: int | None = None  # minutes

    @property
    def key(self) -> tuple[int, int]:
        return (self.season, self.episode)

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"


@dataclass
class ShowData:
    """Everything we fetched about a show."""

    title: str
    imdb_id: str | None
    show_links: dict[str, str]  # service -> show-level url
    episodes: list[Episode]  # sorted
    status: str | None = None  # TVmaze: Running / Ended / ...
    season_sizes: dict[int, int] = field(default_factory=dict)  # planned episodes per season
    poster: str | None = None

    @property
    def ended(self) -> bool:
        return (self.status or "").lower() == "ended"

    def finale(self, ep: Episode) -> str | None:
        """'series' / 'season' if ep is a finale, else None."""
        last_season = max((e.season for e in self.episodes), default=ep.season)
        size = self.season_sizes.get(ep.season)
        if size:
            is_last = ep.episode >= size
        else:
            in_season = [e.episode for e in self.episodes if e.season == ep.season]
            # Without a planned count, only trust "last listed" once a later season exists.
            is_last = ep.season < last_season and ep.episode == max(in_season)
        if not is_last:
            return None
        if self.ended and ep.season == last_season:
            return "series"
        return "season"

    def last_aired(self, now: datetime) -> Episode | None:
        """The most recent episode that is out (has a link or aired)."""
        out = [e for e in self.episodes if e.links or (e.airstamp and e.airstamp <= now)]
        return out[-1] if out else None

    def find(self, season: int, episode: int) -> Episode | None:
        for ep in self.episodes:
            if ep.key == (season, episode):
                return ep
        return None

    def after(self, key: tuple[int, int] | None) -> Episode | None:
        """The first episode after key (or the very first one)."""
        for ep in self.episodes:
            if key is None or ep.key > key:
                return ep
        return None

    def before(self, key: tuple[int, int]) -> Episode | None:
        prev = None
        for ep in self.episodes:
            if ep.key >= key:
                return prev
            prev = ep
        return prev


class Catalog:
    """Thin async client for JustWatch + TVmaze."""

    def __init__(self, session: aiohttp.ClientSession, country: str, language: str) -> None:
        self._session = session
        self.country = country.upper()
        self.language = language.lower()

    async def _jw(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        variables = {"country": self.country, "language": self.language, **variables}
        try:
            async with self._session.post(
                JW_URL, json={"query": query, "variables": variables}, timeout=TIMEOUT
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CatalogError(f"JustWatch request failed: {err}") from err
        if data.get("errors"):
            raise CatalogError(f"JustWatch error: {data['errors'][0].get('message')}")
        return data.get("data") or {}

    async def search(self, text: str) -> list[SearchResult]:
        fallback = next(c for c in _FALLBACK_SEARCH if c != self.country)
        local, extra = await asyncio.gather(
            self._search_in(self.country, text),
            self._search_in(fallback, text),
            return_exceptions=True,
        )
        if isinstance(local, BaseException):
            raise local
        if isinstance(extra, BaseException):
            _LOGGER.debug("Fallback search in %s failed: %s", fallback, extra)
            return local
        seen = {r.jw_id for r in local}
        # Only shows that stream here; the rest would be noise.
        return local + [r for r in extra if r.jw_id not in seen and r.services]

    async def _search_in(self, search_country: str, text: str) -> list[SearchResult]:
        data = await self._jw(
            _SEARCH_QUERY, {"searchCountry": search_country, "filter": {"searchQuery": text}}
        )
        results: list[SearchResult] = []
        for edge in (data.get("popularTitles") or {}).get("edges", []):
            node = edge["node"]
            if node.get("objectType") != "SHOW":
                continue
            content = node.get("content") or {}
            services: dict[str, str] = {}
            for offer in node.get("offers") or []:
                if offer.get("monetizationType") in _PAID:
                    pkg = offer["package"]
                    services[pkg["technicalName"]] = pkg["clearName"]
            results.append(
                SearchResult(
                    jw_id=node["id"],
                    title=content.get("title") or "?",
                    year=content.get("originalReleaseYear"),
                    imdb_id=(content.get("externalIds") or {}).get("imdbId"),
                    services=services,
                    poster=poster_url(content.get("posterUrl")),
                )
            )
        return results

    async def show(self, jw_id: str) -> ShowData:
        data = await self._jw(_SHOW_QUERY, {"id": jw_id})
        node = data.get("node")
        if not node:
            raise CatalogError(f"Show {jw_id} not found")
        content = node.get("content") or {}
        show_links: dict[str, str] = {}
        for offer in node.get("offers") or []:
            if offer.get("monetizationType") in _PAID and offer.get("standardWebURL"):
                show_links.setdefault(
                    offer["package"]["technicalName"], clean_url(offer["standardWebURL"])
                )
        episodes: list[Episode] = []
        for season in node.get("seasons") or []:
            snum = (season.get("content") or {}).get("seasonNumber")
            if not snum:  # skip specials (season 0)
                continue
            for raw in season.get("episodes") or []:
                ec = raw.get("content") or {}
                enum = ec.get("episodeNumber")
                if enum is None:
                    continue
                ep = Episode(season=int(snum), episode=int(enum), title=ec.get("title"))
                for offer in raw.get("offers") or []:
                    if offer.get("monetizationType") in _PAID and offer.get("standardWebURL"):
                        ep.links.setdefault(
                            offer["package"]["technicalName"],
                            clean_url(offer["standardWebURL"]),
                        )
                episodes.append(ep)
        imdb = (content.get("externalIds") or {}).get("imdbId")
        show = ShowData(
            title=content.get("title") or "?",
            imdb_id=imdb,
            show_links=show_links,
            episodes=sorted(episodes, key=lambda e: e.key),
            poster=poster_url(content.get("posterUrl")),
        )
        if imdb:
            try:
                await self._merge_tvmaze(show, imdb)
            except CatalogError as err:
                _LOGGER.debug("No TVmaze data for %s: %s", show.title, err)
        return show

    async def _tvmaze(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            async with self._session.get(
                f"{TVMAZE_URL}{path}", params=params, timeout=TIMEOUT
            ) as resp:
                if resp.status == 404:
                    raise CatalogError("not found")
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CatalogError(f"TVmaze request failed: {err}") from err

    async def _merge_tvmaze(self, show: ShowData, imdb: str) -> None:
        """Add air dates, plus episodes JustWatch doesn't list yet (announced ones)."""
        info = await self._tvmaze("/lookup/shows", {"imdb": imdb})
        show.status = info.get("status")
        ends: dict[int, str] = {}  # season -> last air date, when no episode count is given
        try:
            for season in await self._tvmaze(f"/shows/{info['id']}/seasons"):
                if season.get("number") and season.get("episodeOrder"):
                    show.season_sizes[int(season["number"])] = int(season["episodeOrder"])
                elif season.get("number") and season.get("endDate"):
                    ends[int(season["number"])] = season["endDate"]
        except CatalogError as err:
            _LOGGER.debug("No season info for %s: %s", show.title, err)
        eps = await self._tvmaze(f"/shows/{info['id']}/episodes")
        by_key = {ep.key: ep for ep in show.episodes}
        for raw in eps:
            if raw.get("season") is None or raw.get("number") is None:
                continue
            key = (int(raw["season"]), int(raw["number"]))
            stamp = None
            if raw.get("airstamp"):
                try:
                    stamp = datetime.fromisoformat(raw["airstamp"])
                except ValueError:
                    stamp = None
            runtime = raw.get("runtime") if isinstance(raw.get("runtime"), int) else None
            if key in by_key:
                by_key[key].airstamp = stamp
                by_key[key].title = by_key[key].title or raw.get("name")
                by_key[key].runtime = runtime
            else:
                ep = Episode(season=key[0], episode=key[1], title=raw.get("name"), airstamp=stamp,
                             runtime=runtime)
                show.episodes.append(ep)
                by_key[key] = ep
        for num, end in ends.items():
            # No planned count: the episode airing on the season's end date closes it.
            last = [int(r["number"]) for r in eps
                    if r.get("season") == num and r.get("number") and r.get("airdate") == end]
            if last:
                show.season_sizes[num] = max(last)
        show.episodes.sort(key=lambda e: e.key)
