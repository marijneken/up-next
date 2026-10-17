"""Tests for Up Next."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.up_next.api import CatalogError, Episode, SearchResult, ShowData
from custom_components.up_next.const import DOMAIN

NOW = dt_util.utcnow()


def lasso() -> ShowData:
    """S1 (3 eps, done), S2E1 out, S2E2 next week. Apple TV+."""
    eps = [
        Episode(1, e, f"Ep {e}", {"appletvplus": f"https://tv.apple.com/x?playableId=A00{e}"},
                NOW - timedelta(days=60 - e))
        for e in range(1, 4)
    ]
    eps.append(Episode(2, 1, "Back", {"appletvplus": "https://tv.apple.com/y?playableId=B001"},
                       NOW - timedelta(days=1)))
    eps.append(Episode(2, 2, "Later", {}, NOW + timedelta(days=6)))
    return ShowData("Ted Lasso", "tt1", {"appletvplus": "https://tv.apple.com/show"}, eps,
                    "Running", {1: 3, 2: 2})


def silo() -> ShowData:
    """Netflix-ish show on a season break, S1 complete."""
    eps = [Episode(1, e, f"Silo {e}", {"netflix": "https://www.netflix.com/title/1"},
                   NOW - timedelta(days=100 - e)) for e in range(1, 3)]
    return ShowData("Silo", "tt2", {"netflix": "https://www.netflix.com/title/1"}, eps,
                    "Running", {1: 2})


CATALOG = {"ts1": lasso, "ts2": silo}


async def fake_show(self, jw_id):  # noqa: ANN001
    return CATALOG[jw_id]()


@pytest.fixture(autouse=True)
def catalog():
    with patch("custom_components.up_next.api.Catalog.show", fake_show), \
         patch("custom_components.up_next.manager.asyncio.sleep", AsyncMock()):
        yield


@pytest.fixture
def atv(hass: HomeAssistant):
    """Fake Apple TV entities on one device."""
    atv_entry = MockConfigEntry(domain="apple_tv", entry_id="atv_entry")
    atv_entry.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=atv_entry.entry_id, identifiers={("apple_tv", "x")}
    )
    reg = er.async_get(hass)
    # like the real thing: every entity's unique_id is the Apple TV's MAC
    for domain, obj, tkey in (("media_player", "atv", None), ("remote", "atv", None),
                              ("binary_sensor", "kb", "keyboard_focused")):
        reg.async_get_or_create(domain, "apple_tv", "82:21:61:F7:E6:EA", config_entry=atv_entry,
                                device_id=dev.id, suggested_object_id=obj,
                                translation_key=tkey)
    hass.states.async_set("media_player.atv", "idle", {})
    hass.states.async_set("binary_sensor.kb", "on")
    return atv_entry


def sub(title: str, jw: str, last=None) -> ConfigSubentryData:  # noqa: ANN001
    return ConfigSubentryData(
        data={"title": title, "jw_id": jw, "imdb_id": None, "last_watched": last},
        subentry_type="show", title=title, unique_id=None,
    )


async def setup(hass: HomeAssistant, subs: list, subscriptions=None) -> MockConfigEntry:  # noqa: ANN001
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"media_player": "media_player.atv", "remote": "remote.atv", "country": "NL",
              "language": "en", "announce_entity": "media_player.echo",
              "subscriptions": subscriptions or ["netflix", "amazonprimevideo", "appletvplus"]},
        subentries_data=subs,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def tracker(entry, title):  # noqa: ANN001
    return next(s for s in entry.runtime_data.shows.values() if s.title == title)


async def test_entities(hass: HomeAssistant, atv) -> None:
    await setup(hass, [sub("Ted Lasso", "ts1", [1, 3]), sub("Silo", "ts2", [1, 2])])
    assert hass.states.get("sensor.up_next_up_next").state == "Ted Lasso S02E01"
    assert hass.states.get("button.up_next_play_next_episode") is not None
    assert hass.states.get("todo.up_next_queue") is None
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), next(iter(
        hass.config_entries.async_entries(DOMAIN))).entry_id)
    assert len(devices) == 1


async def test_priority_fallthrough_and_finale(hass: HomeAssistant, atv) -> None:
    # Silo first in queue but caught up; Ted Lasso S1E3 (season 1 finale) next.
    entry = await setup(hass, [sub("Silo", "ts2", [1, 2]), sub("Ted Lasso", "ts1", [1, 2])])
    play = async_mock_service(hass, "media_player", "play_media")
    keys = async_mock_service(hass, "remote", "send_command")
    tts = async_mock_service(hass, "notify", "alexa_media")
    await hass.services.async_call("button", "press", {"entity_id": "button.up_next_play_next_episode"},
                                   blocking=True)
    await hass.async_block_till_done()
    assert play[0].data["media_content_id"].endswith("playableId=A003")
    assert keys[0].data["command"] == ["select"]
    msg = tts[0].data["message"]
    assert "Silo" not in msg  # caught up = on a break: not mentioned
    assert "season 1 finale" in msg
    assert tracker(entry, "Ted Lasso").in_progress == (1, 3)


async def test_panel_websocket(hass: HomeAssistant, atv, hass_ws_client) -> None:
    entry = await setup(hass, [sub("Ted Lasso", "ts1", [1, 3]), sub("Silo", "ts2", None)])
    mgr = entry.runtime_data
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "up_next/subscribe"})
    assert (await ws.receive_json())["success"]
    snap = (await ws.receive_json())["event"]
    assert [s["title"] for s in snap["shows"]] == ["Ted Lasso", "Silo"]
    assert snap["shows"][0]["mode"] == "latest" and snap["shows"][0]["playable"]
    silo_id = snap["shows"][1]["id"]

    await ws.send_json({"id": 2, "type": "up_next/move", "show_id": silo_id, "after": None})
    msgs = [await ws.receive_json(), await ws.receive_json()]
    result = next(m for m in msgs if m.get("id") == 2 and m["type"] == "result")
    assert result["success"]
    assert [s.title for s in mgr.queue] == ["Silo", "Ted Lasso"]

    await ws.send_json({"id": 3, "type": "up_next/episodes", "show_id": silo_id})
    while (m := await ws.receive_json())["id"] != 3:
        pass
    assert [e["key"] for e in m["result"]] == ["1:1", "1:2"]

    await ws.send_json({"id": 4, "type": "up_next/set_progress", "show_id": silo_id,
                        "mode": "custom", "next": "1:2"})
    while (m := await ws.receive_json())["id"] != 4 or m["type"] != "result":
        pass
    assert m["result"]["next"]["code"] == "S01E02"

    results = [SearchResult("ts9", "New", 2024, None, {"netflix": "Netflix"}, None)]
    with patch("custom_components.up_next.api.Catalog.search", AsyncMock(return_value=results)):
        await ws.send_json({"id": 5, "type": "up_next/search", "query": "new"})
        while (m := await ws.receive_json())["id"] != 5 or m["type"] != "result":
            pass
    assert m["result"][0]["mine"] == ["Netflix"]

    CATALOG["ts9"] = silo
    await ws.send_json({"id": 6, "type": "up_next/add", "jw_id": "ts9", "title": "New",
                        "mode": "caught_up"})
    while (m := await ws.receive_json())["id"] != 6 or m["type"] != "result":
        pass
    assert m["success"]
    await hass.async_block_till_done()
    mgr = entry.runtime_data
    assert [s.title for s in mgr.queue] == ["Silo", "Ted Lasso", "New"]
    assert mgr.queue[2].last_watched == (1, 2)

    await ws.send_json({"id": 7, "type": "up_next/remove", "show_id": mgr.queue[2].subentry_id})
    while (m := await ws.receive_json())["id"] != 7 or m["type"] != "result":
        pass
    await hass.async_block_till_done()
    assert [s.title for s in entry.runtime_data.queue] == ["Silo", "Ted Lasso"]


async def test_nothing_new_announces_next(hass: HomeAssistant, atv) -> None:
    await setup(hass, [sub("Ted Lasso", "ts1", [2, 1]), sub("Silo", "ts2", [1, 2])])
    tts = async_mock_service(hass, "notify", "alexa_media")
    resp = await hass.services.async_call(DOMAIN, "play", {}, blocking=True, return_response=True)
    await hass.async_block_till_done()
    assert resp["status"] == "nothing_new"
    assert "Next up: Ted Lasso" in tts[0].data["message"]


async def test_subscription_filter(hass: HomeAssistant, atv) -> None:
    entry = await setup(hass, [sub("Silo", "ts2", None)], subscriptions=["appletvplus"])
    nxt = tracker(entry, "Silo").next()
    assert nxt.status == "unavailable"
    assert hass.states.get("sensor.up_next_up_next").state == "Nothing new"


async def test_tracking(hass: HomeAssistant, atv) -> None:
    entry = await setup(hass, [sub("Ted Lasso", "ts1", [1, 3])])
    show = tracker(entry, "Ted Lasso")
    hass.states.async_set("media_player.atv", "playing", {
        "app_id": "com.apple.TVWatchList", "media_title": "Ted Lasso",
        "media_content_id": "B001", "media_position": 2950, "media_duration": 3000})
    await hass.async_block_till_done()
    assert show.last_watched == (2, 1)
    assert show.next().status == "upcoming"


async def test_prime_artist_parsing(hass: HomeAssistant, atv) -> None:
    entry = await setup(hass, [sub("Ted Lasso", "ts1", None)])
    show = tracker(entry, "Ted Lasso")
    key = show.identify({"app_id": "com.amazon.aiv.AIVApp", "media_title": "Ted Lasso",
                         "media_artist": "Season 1, Ep. 3 “Trent Crimm”"})
    assert key == (1, 3)


async def test_config_and_show_flows(hass: HomeAssistant, atv) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"media_player": "media_player.atv", "subscriptions": ["netflix"],
                            "country": "nl", "language": "EN"})
    assert result["type"] == "create_entry"
    assert result["data"]["remote"] == "remote.atv"
    entry = result["result"]
    await hass.async_block_till_done()

    results = [SearchResult("ts2", "Silo", 2023, "tt2", {"netflix": "Netflix"})]
    with patch("custom_components.up_next.api.Catalog.search", AsyncMock(return_value=results)):
        flow = await hass.config_entries.subentries.async_init((entry.entry_id, "show"), context={"source": "user"})
        flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"query": "silo"})
        flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"result": "ts2"})
        assert flow["step_id"] == "progress"
        assert "Netflix" in flow["description_placeholders"]["where"]
        flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"start": "caught_up"})
        assert flow["type"] == "create_entry"
        await hass.async_block_till_done()
    show = tracker(entry, "Silo")
    assert show.last_watched == (1, 2) and show.next().status == "waiting"

    # reconfigure: pick a specific episode
    sid = show.subentry_id
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "show"), context={"source": "reconfigure", "subentry_id": sid})
    assert flow["step_id"] == "reconfigure"
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"start": "custom"})
    assert flow["step_id"] == "episode"
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"next_episode": "1:2"})
    assert flow["type"] == "abort"
    await hass.async_block_till_done()
    show = tracker(entry, "Silo")
    assert show.next().episode.key == (1, 2)


async def test_entry_titles_and_no_reload_loop(hass: HomeAssistant, atv) -> None:
    entry = await setup(hass, [sub("Ted Lasso", "ts1", [1, 3]), sub("Silo", "ts2", [1, 2])])
    mgr = entry.runtime_data
    await hass.async_block_till_done()
    titles = sorted(s.title for s in entry.subentries.values())
    assert titles[0].startswith("Silo — caught up after the season finale")
    assert titles[1] == "Ted Lasso — next S02E01 · out now"
    assert entry.runtime_data is mgr  # title updates didn't reload the entry


async def test_reconfigure_defaults_to_current(hass: HomeAssistant, atv) -> None:
    entry = await setup(hass, [sub("Ted Lasso", "ts1", [1, 3])])
    sid = next(iter(entry.subentries))
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "show"), context={"source": "reconfigure", "subentry_id": sid})
    default = flow["data_schema"].schema
    key = next(k for k in default if str(k) == "start")
    assert key.default() == "latest"  # next is S2E1 = the latest released episode


# ---------------------------------------------------------------- v0.4

from datetime import date as _date  # noqa: E402

from custom_components.up_next.schedule import Lane, latest_start  # noqa: E402


def _check_gapless(higher, target, finale, d0, start):
    from custom_components.up_next.schedule import _gaps
    lanes = [*higher, Lane("T", target, start)]
    waits, began, watched = _gaps(lanes, "T", finale + 1, d0, d0 + timedelta(days=400))
    assert waits == 0
    assert began == start  # reported start = first evening it actually plays
    if start > d0:  # one day earlier would mean waiting at some point
        earlier = [*higher, Lane("T", target, start - timedelta(days=1))]
        assert _gaps(earlier, "T", finale + 1, d0, d0 + timedelta(days=400))[0] > 0
    return watched


def test_latest_start_never_waits_for_an_episode() -> None:
    d0 = _date(2026, 10, 7)
    day = lambda n: d0 + timedelta(days=n)  # noqa: E731
    higher = [Lane("A", [day(2), day(9), day(16), day(23)], d0)]  # weekly show above us
    target = [day(-30)] * 4 + [day(5), day(12), day(19), day(26)]  # 4 out, 4 weekly; finale idx 7
    start, watch = latest_start(higher, target, 7, d0)
    assert start > d0
    assert _check_gapless(higher, target, 7, d0, start) == watch
    assert watch >= day(26)


def test_latest_start_with_backlog_and_same_night_show() -> None:
    """A higher weekly show on the finale weekday plus a higher backlog show."""
    d0 = _date(2026, 10, 7)  # Wednesday
    day = lambda n: d0 + timedelta(days=n)  # noqa: E731
    ted = Lane("ted", [day(0), day(7), day(14)], d0)
    gentlemen = Lane("gent", [day(-100)] * 6, d0)  # 6 episodes to go
    target = [day(-300)] * 4 + [day(7), day(14)]  # finale idx 5, out Wed day 14
    start, watch = latest_start([ted, gentlemen], target, 5, d0)
    assert start > d0
    assert _check_gapless([ted, gentlemen], target, 5, d0, start) == watch
    assert watch > day(14)  # Ted takes the finale's release night


def slowhorses() -> ShowData:
    """S1 done long ago; S2 has 2 eps out and 4 weekly to come (finale S2E6)."""
    eps = [Episode(1, e, f"S1 {e}", {"appletvplus": f"https://tv.apple.com/a?playableId=X1{e}"},
                   NOW - timedelta(days=400 - e)) for e in range(1, 4)]
    for e in range(1, 7):
        aired = e <= 2
        eps.append(Episode(2, e, f"S2 {e}",
                           {"appletvplus": f"https://tv.apple.com/a?playableId=X2{e}"} if aired else {},
                           NOW + timedelta(days=(e - 2) * 7) - timedelta(hours=1)))
    return ShowData("Slow Horses", "tt3", {"appletvplus": "https://tv.apple.com/show"}, eps,
                    "Running", {1: 3, 2: 6})


async def test_season_start_mode_and_auto_hold(hass: HomeAssistant, atv) -> None:
    CATALOG["ts3"] = slowhorses
    entry = await setup(hass, [sub("Slow Horses", "ts3", [1, 3])])
    mgr = entry.runtime_data
    show = tracker(entry, "Slow Horses")
    from custom_components.up_next.manager import current_mode, last_watched_for
    assert last_watched_for(show.data, "season_start") == [1, 3]
    assert current_mode(show) == "season_start"
    assert mgr.up_next()[0] is show

    mgr.set_hold(show, "auto")
    plan = mgr.plans[show.subentry_id]
    assert plan.finale == dt_util.as_local(show.data.find(2, 6).airstamp).date()
    assert plan.start > dt_util.now().date()  # 6 eps, finale in 4 weeks: no need to start yet
    assert mgr.up_next() is None
    assert "auto-starts" in show.short_status()

    mgr.set_hold(show, "manual")
    assert mgr.up_next() is None and show.short_status().startswith("on hold")
    mgr.set_hold(show, None)
    assert mgr.up_next()[0] is show


async def test_secondary_list(hass: HomeAssistant, atv) -> None:
    entry = await setup(hass, [sub("Ted Lasso", "ts1", [1, 3]), sub("Silo", "ts2", None)])
    mgr = entry.runtime_data
    silo = tracker(entry, "Silo")
    mgr.set_list(silo, "secondary")
    assert [s.title for s in mgr.list_queue("main")] == ["Ted Lasso"]
    assert mgr.up_next("secondary")[0] is silo
    play = async_mock_service(hass, "media_player", "play_media")
    async_mock_service(hass, "remote", "send_command")
    async_mock_service(hass, "apple_tv", "set_keyboard_text")
    await hass.services.async_call("button", "press",
                                   {"entity_id": "button.up_next_play_secondary_show"}, blocking=True)
    assert play[0].data["media_content_id"] == "https://www.netflix.com/title/1"
    assert hass.states.get("sensor.up_next_up_next_secondary") is not None


async def test_panel_survives_reload(hass: HomeAssistant, atv, hass_ws_client) -> None:
    """After adding a show the entry reloads; the panel must get a ready snapshot."""
    entry = await setup(hass, [sub("Ted Lasso", "ts1", [1, 3])])
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "up_next/subscribe"})
    await ws.receive_json()
    await ws.receive_json()
    await ws.send_json({"id": 2, "type": "up_next/add", "jw_id": "ts2", "title": "Silo",
                        "mode": "beginning", "list": "secondary", "hold": "manual"})
    await hass.async_block_till_done()
    last = None
    while True:
        try:
            m = await ws.receive_json(timeout=0.5)
        except TimeoutError:
            break
        if m.get("type") == "event":
            last = m["event"]
    assert last and last["ready"]
    silo = next(s for s in last["shows"] if s["title"] == "Silo")
    assert silo["list"] == "secondary" and silo["hold"] == "manual"
    assert entry.runtime_data.list_queue("secondary")[0].title == "Silo"


async def test_tvmaze_end_date_marks_finale() -> None:
    """No episodeOrder (Ted Lasso S4): the episode on the season's endDate is the finale."""
    from custom_components.up_next.api import Catalog

    replies = {
        "/lookup/shows": {"id": 1, "status": "Running"},
        "/shows/1/seasons": [{"number": 4, "episodeOrder": None, "endDate": "2026-10-07"}],
        "/shows/1/episodes": [
            {"season": 4, "number": n, "airdate": f"2026-09-{n * 3:02d}" if n < 10 else "2026-10-07",
             "airstamp": None, "name": f"E{n}"} for n in range(1, 11)
        ],
    }
    cat = Catalog(None, "NL", "en")

    async def fake(path, params=None):
        return replies[path]

    cat._tvmaze = fake
    show = ShowData("Ted Lasso", "tt1", {}, [], "Running", {})
    await cat._merge_tvmaze(show, "tt1")
    assert show.finale(show.find(4, 10)) == "season"
    assert show.finale(show.find(4, 9)) is None


async def test_search_falls_back_to_bigger_catalog() -> None:
    """A show JustWatch NL search misses is found via US, if it streams in NL."""
    from custom_components.up_next.api import Catalog

    def node(jw, title, pkgs):  # noqa: ANN001
        offers = [{"monetizationType": "FLATRATE", "package": {"technicalName": p, "clearName": p}}
                  for p in pkgs]
        return {"node": {"id": jw, "objectType": "SHOW", "offers": offers,
                         "content": {"title": title, "originalReleaseYear": 2026}}}

    replies = {
        "NL": [node("ts1", "Scaredy Cats", ["netflix"])],
        "US": [node("ts2", "Alley Cats", ["netflix"]), node("ts1", "Scaredy Cats", ["netflix"]),
               node("ts3", "US only", [])],
    }
    cat = Catalog(None, "NL", "en")
    calls = []

    async def fake(query, variables):  # noqa: ANN001
        calls.append(variables["searchCountry"])
        return {"popularTitles": {"edges": replies[variables["searchCountry"]]}}

    cat._jw = fake
    assert [r.title for r in await cat.search("alley cats")] == ["Scaredy Cats", "Alley Cats"]
    assert sorted(calls) == ["NL", "US"]

    async def us_down(query, variables):  # noqa: ANN001
        if variables["searchCountry"] == "US":
            raise CatalogError("down")
        return await fake(query, variables)

    cat._jw = us_down
    assert [r.title for r in await cat.search("alley cats")] == ["Scaredy Cats"]


async def test_break_hides_and_comeback_notifies(hass: HomeAssistant, atv, hass_ws_client) -> None:
    entry = await setup(hass, [sub("Silo", "ts2", [1, 2]), sub("Ted Lasso", "ts1", [1, 3])])
    mgr = entry.runtime_data
    silo_t, ted = tracker(entry, "Silo"), tracker(entry, "Ted Lasso")
    assert silo_t.on_break() and not ted.on_break()
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "up_next/subscribe"})
    res = await ws.receive_json()
    snap = res.get("event") or (await ws.receive_json())["event"]
    assert {s["title"]: s["on_break"] for s in snap["shows"]} == {"Silo": True, "Ted Lasso": False}
    queue = hass.states.get("sensor.up_next_up_next").attributes["queue"]
    assert [q["show"] for q in queue] == ["Ted Lasso"]
    # far-off announced episode: still on a break
    silo_t.data.episodes.append(Episode(2, 1, "S2", {}, NOW + timedelta(days=200)))
    mgr.notify()
    assert silo_t.on_break()
    # new season out: back, with a notification and a spoken mention next time
    pn = []
    patcher = patch("custom_components.up_next.manager.persistent_notification.async_create",
                    lambda hass, msg, **kw: pn.append(msg))
    patcher.start()
    silo_t.data.episodes[-1] = Episode(2, 1, "S2", {"netflix": "https://www.netflix.com/title/1"},
                                       NOW - timedelta(hours=2))
    mgr.notify()
    await hass.async_block_till_done()
    assert not silo_t.on_break()
    patcher.stop()
    assert "Silo is back" in pn[0]
    async_mock_service(hass, "media_player", "play_media")
    async_mock_service(hass, "remote", "send_command")
    async_mock_service(hass, "apple_tv", "set_keyboard_text")
    tts = async_mock_service(hass, "notify", "alexa_media")
    await mgr.async_play(ted)
    assert "Silo is back" in tts[0].data["message"]
    await mgr.async_play(ted)  # said once
    assert len(tts) == 1 or "Silo is back" not in tts[-1].data["message"]


async def test_volume_learned_and_restored(hass: HomeAssistant, atv, hass_ws_client) -> None:
    entry = await setup(hass, [sub("Ted Lasso", "ts1", [1, 3])])
    show = tracker(entry, "Ted Lasso")
    attrs = {"app_id": "com.apple.TVWatchList", "media_title": "Ted Lasso", "media_content_id": "B001",
             "media_duration": 3000, "volume_level": 0.35}
    hass.states.async_set("media_player.atv", "playing", {**attrs, "media_position": 300})
    await hass.async_block_till_done()
    assert show.volume is None  # 10% in: not settled yet
    hass.states.async_set("media_player.atv", "playing", {**attrs, "media_position": 650})
    await hass.async_block_till_done()
    assert show.volume == 0.35
    hass.states.async_set("media_player.atv", "playing", {**attrs, "media_position": 900, "volume_level": 0.6})
    await hass.async_block_till_done()
    assert show.volume == 0.35  # once per episode
    hass.states.async_set("media_player.atv", "idle", {})
    vol_set = async_mock_service(hass, "media_player", "volume_set")
    async_mock_service(hass, "media_player", "play_media")
    async_mock_service(hass, "remote", "send_command")
    async_mock_service(hass, "notify", "alexa_media")
    await entry.runtime_data.async_play(show)
    assert vol_set[0].data["volume_level"] == 0.35
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "up_next/set_volume", "show_id": show.subentry_id,
                        "mode": "fixed", "volume": 0.5})
    res = await ws.receive_json()
    assert res["success"] and res["result"]["volume"] == 0.5 and res["result"]["volume_mode"] == "fixed"
    await ws.send_json({"id": 2, "type": "up_next/set_volume", "show_id": show.subentry_id, "mode": "off"})
    assert (await ws.receive_json())["success"]
    await entry.runtime_data.async_play(show)
    assert len(vol_set) == 1


async def test_auto_start_show_with_nothing_announced(hass: HomeAssistant, atv) -> None:
    """Auto-start on a show you're caught up on (no next season yet) must not break setup."""
    entry = await setup(hass, [sub("Silo", "ts2", [1, 2])])
    show = tracker(entry, "Silo")
    entry.runtime_data.set_hold(show, "auto")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.value == "loaded"
    assert tracker(entry, "Silo").on_break()


async def _netflix_start(hass, entry, focus="on"):  # noqa: ANN001
    hass.states.async_set("binary_sensor.kb", focus)
    hass.states.async_set("media_player.atv", "idle", {"volume_level": 0.4})
    calls = {
        "play": async_mock_service(hass, "media_player", "play_media"),
        "vol": async_mock_service(hass, "media_player", "volume_set"),
        "keys": async_mock_service(hass, "remote", "send_command"),
        "text": async_mock_service(hass, "apple_tv", "set_keyboard_text"),
        "tts": async_mock_service(hass, "notify", "alexa_media"),
    }
    await entry.runtime_data.async_play(tracker(entry, "Silo"))
    return calls


async def test_netflix_quiet_launch_and_typing(hass: HomeAssistant, atv) -> None:
    entry = await setup(hass, [sub("Silo", "ts2", [1, 1])])
    calls = await _netflix_start(hass, entry)
    levels = [c.data["volume_level"] for c in calls["vol"]]
    assert levels == [0, 0.4]  # quiet for both app starts, back before "Play"
    assert len(calls["play"]) == 2
    assert [c.data["text"] for c in calls["text"]] == ["Silo", "Silo"]
    # the learned volume wins over the old level when it's known
    tracker(entry, "Silo")._p["volume"] = 0.3
    calls2 = await _netflix_start(hass, entry)
    assert [c.data["volume_level"] for c in calls2["vol"]] == [0, 0.3]


async def test_netflix_slow_start_restarts_never_select_after_up_left(
    hass: HomeAssistant, atv
) -> None:
    """Slow Netflix: no focus after select, up+left -> close and redo, never extra presses."""
    entry = await setup(hass, [sub("Silo", "ts2", [1, 1])])
    hass.states.async_set("binary_sensor.kb", "off")
    hass.states.async_set("media_player.atv", "idle", {"volume_level": 0.4})
    play = async_mock_service(hass, "media_player", "play_media")
    async_mock_service(hass, "media_player", "volume_set")
    text = async_mock_service(hass, "apple_tv", "set_keyboard_text")
    async_mock_service(hass, "notify", "alexa_media")
    keys = []

    async def remote(call):  # noqa: ANN001
        keys.append(call.data["command"])
        if call.data["command"] == ["down"] and keys.count(["down"]) == 2:
            hass.states.async_set("binary_sensor.kb", "on")  # second try: keyboard up

    hass.services.async_register("remote", "send_command", remote)
    await entry.runtime_data.async_play(tracker(entry, "Silo"))
    before_typing = keys[: keys.index(["up", "left"], keys.index(["up", "left"]) + 1) + 1]
    # never "select" straight after "up, left" (that's the profile editor)
    for prev, cur in zip(before_typing, before_typing[1:]):
        assert not (prev == ["up", "left"] and cur == ["select"])
    assert keys.count(["home", "home"]) == 2  # closed Netflix twice (recipe + retry)
    # each try: select, up+left, then down (which brings up the keyboard)
    assert keys[:3] == [["home", "home"], ["up", "up"], ["menu"]]
    tries = [i for i, k in enumerate(keys) if k == ["select"]][:2]
    for i in tries:
        assert keys[i:i + 3] == [["select"], ["up", "left"], ["down"]]
    assert len(play) == 3  # opened, reopened, reopened after the slow start
    assert [c.data["text"] for c in text] == ["Silo", "Silo"]


async def test_netflix_never_shows_search_gives_up(hass: HomeAssistant, atv) -> None:
    entry = await setup(hass, [sub("Silo", "ts2", [1, 1])])
    with pytest.raises(HomeAssistantError):
        await _netflix_start(hass, entry, focus="off")


async def test_failed_launch_keeps_session_and_volume(hass: HomeAssistant, atv) -> None:
    entry = await setup(hass, [sub("Silo", "ts2", [1, 1])])
    show = tracker(entry, "Silo")
    hass.states.async_set("media_player.atv", "idle", {"volume_level": 0.4})
    vol = async_mock_service(hass, "media_player", "volume_set")
    tts = async_mock_service(hass, "notify", "alexa_media")

    async def boom(call):  # noqa: ANN001
        raise HomeAssistantError("stuck")

    hass.services.async_register("media_player", "play_media", boom)
    with pytest.raises(HomeAssistantError):
        await entry.runtime_data.async_play(show)
    assert entry.runtime_data.last_started == show.subentry_id
    assert show.in_progress == (1, 2)
    assert vol[-1].data["volume_level"] == 0.4  # not left silent
    assert "couldn't start Silo" in tts[-1].data["message"]


async def test_netflix_counts_playing_time(hass: HomeAssistant, atv, freezer) -> None:
    entry = await setup(hass, [sub("Silo", "ts2", [1, 0])])
    show = tracker(entry, "Silo")
    show.data.episodes[0].runtime = 50
    mgr = entry.runtime_data
    show._set(in_progress=(1, 1), started_at=dt_util.utcnow(), service="netflix")
    mgr.last_started = show.subentry_id
    netflix = {"app_id": "com.netflix.Netflix"}
    hass.states.async_set("media_player.atv", "playing", netflix)
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=30))
    hass.states.async_set("media_player.atv", "paused", netflix)
    await hass.async_block_till_done()
    assert show.last_watched != (1, 1)  # 30 of 50 min
    hass.states.async_set("media_player.atv", "playing", netflix)
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=16))  # 46 min > 90%
    mgr._tick(dt_util.utcnow())
    assert show.last_watched == (1, 1)
    assert show.in_progress == (1, 2)  # autoplay carries on to the next one


async def test_netflix_volume_learned_without_state_changes(
    hass: HomeAssistant, atv, freezer
) -> None:
    """Netflix sends one state change at the start and then nothing: learn on the tick."""
    entry = await setup(hass, [sub("Silo", "ts2", [1, 0])])
    show = tracker(entry, "Silo")
    mgr = entry.runtime_data
    show._set(in_progress=(1, 1), started_at=dt_util.utcnow(), service="netflix")
    mgr.last_started = show.subentry_id
    hass.states.async_set("media_player.atv", "playing",
                          {"app_id": "com.netflix.Netflix", "volume_level": 0.27})
    await hass.async_block_till_done()
    mgr._tick(dt_util.utcnow())
    assert show.volume is None  # just started
    freezer.tick(timedelta(minutes=11))
    mgr._tick(dt_util.utcnow())
    assert show.volume == 0.27


async def test_netflix_typing_survives_flickering_focus(hass: HomeAssistant, atv) -> None:
    entry = await setup(hass, [sub("Silo", "ts2", [1, 1])])
    hass.states.async_set("binary_sensor.kb", "on")
    hass.states.async_set("media_player.atv", "idle", {"volume_level": 0.4})
    for svc in ("play_media", "volume_set"):
        async_mock_service(hass, "media_player", svc)
    async_mock_service(hass, "remote", "send_command")
    async_mock_service(hass, "notify", "alexa_media")
    tries = []

    async def flaky(call):  # noqa: ANN001
        tries.append(call.data["text"])
        if len(tries) == 1:
            raise HomeAssistantError("No text input field is currently focused on the Apple TV")

    hass.services.async_register("apple_tv", "set_keyboard_text", flaky)
    await entry.runtime_data.async_play(tracker(entry, "Silo"))
    assert tries == ["Silo", "Silo", "Silo"]  # failed, typed, typed again
