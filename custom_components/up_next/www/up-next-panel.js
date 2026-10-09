// Up Next sidebar panel — vanilla web component (no build step).
// Talks to the integration over the HA websocket (up_next/* commands).

const MODES = [
  ["caught_up", "I'm caught up — wait for the next new episode"],
  ["latest", "Play the latest released episode next"],
  ["season_start", "Start the newest season from its first episode"],
  ["beginning", "Haven't started — begin at S1E1"],
  ["custom", "A specific episode…"],
];
const HOLDS = [
  ["none", "Now", "Plays as soon as it has the top spot with a new episode."],
  ["auto", "Auto-start", "Starts on the earliest day from which you never have to wait for the next episode, right up to the season finale (one episode a night, higher shows first)."],
  ["manual", "Hold", "Stays in the list but is skipped until you change this."],
];
const LISTS = [
  ["main", "Main list", "Play next episode", "up_next"],
  ["secondary", "Secondary list", "Play secondary show", "up_next_secondary"],
];

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const code = (key) => {
  const [s, e] = String(key).split(":").map(Number);
  return `S${String(s).padStart(2, "0")}E${String(e).padStart(2, "0")}`;
};

// What each "where are you" choice would make the next episode.
function previews(eps) {
  if (!eps || !eps.length) return {};
  const outs = eps.filter((e) => e.out);
  const last = outs[outs.length - 1];
  const idx = last ? eps.indexOf(last) : -1;
  const after = eps[idx + 1];
  const season = last ? last.key.split(":")[0] : null;
  const first = season ? eps.find((e) => e.key.split(":")[0] === season) : null;
  return {
    caught_up: after ? `next: ${code(after.key)}${after.out ? "" : `, out ${after.air_text || "later"}`}` : "nothing announced yet",
    latest: last ? code(last.key) : "nothing out yet",
    season_start: first ? code(first.key) : "",
    beginning: code(eps[0].key),
  };
}

const fmtDay = (iso) => {
  if (!iso) return "?";
  const d = new Date(iso + "T12:00:00");
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
};

class UpNextPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = { ready: false, shows: [] };
    this._editing = null; // show id whose editor is open
    this._edit = {}; // pending editor values {mode, next, hold, list}
    this._episodes = {}; // id/jw_id -> episode list
    this._adding = null; // {list, query, results, picked, mode, next, hold, busy, error}
    this._dragging = false;
    this._busy = {};
    this._toast = "";
    try { this._showBreak = localStorage.getItem("up_next_show_break") === "1"; } catch (e) { this._showBreak = false; }
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._connect();
  }
  set narrow(v) {
    this._narrow = v;
    this._render();
  }
  set panel(v) {}

  async _connect() {
    this._render();
    try {
      this._unsub = await this._hass.connection.subscribeMessage(
        (msg) => {
          if (!msg.ready && this._data.ready) return; // transient (integration reloading)
          this._data = msg;
          if (!this._dragging) this._render();
        },
        { type: "up_next/subscribe" }
      );
    } catch (e) {
      this._data = { ready: false, shows: [], error: e.message || String(e) };
      this._render();
    }
  }

  disconnectedCallback() {
    if (this._unsub) this._unsub();
    this._unsub = null;
    this._hass = null;
  }
  connectedCallback() {
    if (this._hass && !this._unsub) this._connect();
  }

  _ws(msg) {
    return this._hass.callWS(msg);
  }

  _say(text) {
    this._toast = text;
    this._render();
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      this._toast = "";
      this._render();
    }, 4000);
  }

  // ---------- rendering ----------
  _render() {
    if (!this.shadowRoot) return;
    const active = this.shadowRoot.activeElement;
    const focusId = active && active.id;
    const caret = active && active.selectionStart;
    const d = this._data;
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="toolbar">
        ${this._narrow ? `<ha-menu-button></ha-menu-button>` : ""}
        <div class="title">Up Next</div>
      </div>
      <div class="content">
        ${!d.ready
          ? `<div class="empty">${d.error ? esc(d.error) : "Up Next isn't set up yet. Add the integration first."}</div>`
          : LISTS.map((l) => this._renderList(...l)).join("")}
      </div>
      ${this._toast ? `<div class="toast">${esc(this._toast)}</div>` : ""}
    `;
    if (focusId) {
      const el = this.shadowRoot.getElementById(focusId);
      if (el) {
        el.focus();
        if (caret != null && el.setSelectionRange) el.setSelectionRange(caret, caret);
      }
    }
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) {
      menu.hass = this._hass;
      menu.narrow = this._narrow;
    }
    this._bind();
  }

  _renderList(name, title, playLabel, nextKey) {
    const d = this._data;
    const all = (d.shows || []).filter((s) => (s.list || "main") === name);
    const resting = all.filter((s) => s.on_break).length;
    const shows = this._showBreak ? all : all.filter((s) => !s.on_break);
    const upNext = shows.find((s) => s.id === d[nextKey]);
    const adding = this._adding && this._adding.list === name;
    return `
      <section class="lane">
        <div class="hero">
          <div class="hero-text">
            <div class="hero-label">${esc(title)}</div>
            ${upNext
              ? `<div class="hero-title">${esc(upNext.title)} · ${esc(upNext.next?.code)}</div>
                 <div class="hero-sub">${esc(upNext.next?.title || "")}${upNext.service ? `${upNext.next?.title ? " · " : ""}${esc(upNext.service)}` : ""}${upNext.next?.finale ? ` · <b>${esc(upNext.next.finale)} finale</b>` : ""}</div>`
              : `<div class="hero-title muted0">${shows.length ? "Nothing new right now" : "No shows yet"}</div>`}
          </div>
          <div class="hero-actions">
            <button class="primary" data-act="play-next" data-list="${name}" ${upNext ? "" : "disabled"}>
              <ha-icon icon="mdi:play"></ha-icon> ${esc(playLabel)}</button>
            <button data-act="add" data-list="${name}"><ha-icon icon="mdi:plus"></ha-icon> Add show</button>
          </div>
        </div>
        ${adding ? this._renderAdd() : ""}
        <div class="list" id="list-${name}" data-list="${name}">
          ${shows.map((s) => this._renderShow(s, all.indexOf(s), d[nextKey])).join("")}
          ${shows.length ? "" : `<div class="dropzone">${all.length ? "Every show here is on a break — drag a show here" : "No shows here yet — add one, or drag a show here"}</div>`}
        </div>
        ${resting ? `<button class="link" data-act="toggle-break">${this._showBreak
          ? `<ha-icon icon="mdi:eye-off-outline"></ha-icon> Hide ${resting} on a break`
          : `<ha-icon icon="mdi:eye-outline"></ha-icon> Show ${resting} on a break`}</button>` : ""}
      </section>`;
  }

  _renderShow(s, i, nextId) {
    const n = s.next;
    const badge =
      s.on_break ? `<span class="badge">On a break${s.status === "upcoming" && n?.air_text ? ` · back ${esc(n.air_text)}` : ""}</span>`
      : s.hold === "manual" ? `<span class="badge">On hold</span>`
      : s.hold === "auto" && !s.active ? `<span class="badge auto">${s.plan?.start ? `Starts ${esc(fmtDay(s.plan.start))}` : "Auto-start"}</span>`
      : s.status === "available" ? `<span class="badge ok">New</span>`
      : s.status === "upcoming" ? `<span class="badge soon">${esc(n?.air_text || "Soon")}</span>`
      : s.status === "finished" ? `<span class="badge">Finished</span>`
      : s.status === "unavailable" ? `<span class="badge warn">Not on your services</span>`
      : s.status === "waiting" ? `<span class="badge">Caught up</span>` : "";
    const fin = n?.finale && s.status !== "finished" && !(s.hold === "auto" && !s.active)
      ? ` <span class="badge fin">${n.finale === "series" ? "Series finale" : "Season finale"}</span>` : "";
    const editing = this._editing === s.id;
    return `
      <div class="card ${s.id === nextId ? "next" : ""} ${s.active && !s.on_break ? "" : "held"}" data-id="${s.id}">
        <div class="row">
          <div class="grip" data-grip="${s.id}" title="Drag to reorder"><ha-icon icon="mdi:drag"></ha-icon></div>
          <div class="rank">${i + 1}</div>
          ${s.poster ? `<img class="poster" src="${esc(s.poster)}" alt="" loading="lazy">` : `<div class="poster ph"><ha-icon icon="mdi:television-classic"></ha-icon></div>`}
          <div class="info">
            <div class="name">${esc(s.title)} ${badge}${fin}</div>
            <div class="line">${esc(s.short)}</div>
            <div class="meta">${[n?.title ? `“${esc(n.title)}”` : "", s.service ? esc(s.service) : "", s.last_watched ? `watched up to ${esc(s.last_watched)}` : ""].filter(Boolean).join(" · ")}</div>
          </div>
          <div class="actions">
            <button class="icon" data-act="play" data-id="${s.id}" title="Play this show's next episode now" ${s.playable && !this._busy[s.id] ? "" : "disabled"}><ha-icon icon="mdi:play"></ha-icon></button>
            <button class="icon ${editing ? "on" : ""}" data-act="edit" data-id="${s.id}" title="Settings"><ha-icon icon="mdi:tune-variant"></ha-icon></button>
            <button class="icon" data-act="remove" data-id="${s.id}" title="Remove"><ha-icon icon="mdi:delete-outline"></ha-icon></button>
          </div>
        </div>
        ${editing ? this._renderEditor(s) : ""}
      </div>`;
  }

  _modeRadios(name, current, eps) {
    const pv = previews(eps);
    return MODES.map(([k, label]) => {
      const extra = k !== "custom" && pv[k] ? ` <span class="pv">(${esc(pv[k])})</span>` : "";
      return `<label class="radio"><input type="radio" name="${name}" value="${k}" ${k === current ? "checked" : ""}> <span>${label}${extra}</span></label>`;
    }).join("");
  }

  _episodeSelect(id, eps, selected) {
    if (!eps) return `<div class="muted">Loading episodes…</div>`;
    return `<select class="ep" id="${id}">${eps.map((e) => `<option value="${e.key}" ${e.key === selected ? "selected" : ""}>${esc(e.label)}${e.out ? "" : ` (out ${esc(e.air_text || "later")})`}</option>`).join("")}</select>`;
  }

  _holdRadios(name, current, s) {
    return HOLDS.map(([k, label, help]) => {
      let extra = "";
      if (k === "auto" && s && s.hold === "auto") {
        extra = s.plan?.start
          ? ` — starts ${esc(fmtDay(s.plan.start))}, finale out ${esc(fmtDay(s.plan.finale))}${s.plan.watch_finale && s.plan.watch_finale !== s.plan.finale ? `, you'll watch it ${esc(fmtDay(s.plan.watch_finale))}` : ""} (${s.plan.episodes} episodes)`
          : " — waiting for the finale date to be announced";
      }
      return `<label class="radio"><input type="radio" name="${name}" value="${k}" ${k === current ? "checked" : ""}> <span><b>${label}</b>${extra}<br><span class="help">${esc(help)}</span></span></label>`;
    }).join("");
  }

  _volumeRadios(s) {
    const e = this._edit;
    const mode = e.volume_mode ?? s.volume_mode ?? "auto";
    const pct = Math.round(100 * (e.volume ?? s.volume ?? 0.5));
    const learned = s.volume_mode === "auto" && s.volume != null
      ? `Learned ${Math.round(100 * s.volume)}%${s.volume_from ? ` (during ${esc(s.volume_from)})` : ""}`
      : "Not learned yet";
    const opts = [
      ["auto", "Auto", `${learned}. Picks up your volume 20% into each episode and sets it next time.`],
      ["fixed", "Fixed", ""],
      ["off", "Don't change", "Leave the volume as it is."],
    ];
    return opts.map(([k, label, help]) => `<label class="radio"><input type="radio" name="ed-vol" value="${k}" ${k === mode ? "checked" : ""}> <span><b>${label}</b>${
      k === "fixed" ? (mode === "fixed" ? ` <input type="range" id="ed-volume" min="0" max="100" step="1" value="${pct}"> <span id="ed-volume-pct">${pct}%</span>` : "")
        : `<br><span class="help">${help}</span>`}</span></label>`).join("");
  }

  _renderEditor(s) {
    const e = this._edit;
    const eps = this._episodes[s.id];
    const mode = e.mode ?? s.mode;
    const otherList = (s.list || "main") === "main" ? ["secondary", "Move to secondary list"] : ["main", "Move to main list"];
    return `
      <div class="editor">
        <div class="cols">
          <div>
            <div class="ed-title">Where are you?</div>
            ${this._modeRadios("ed-mode", mode, eps)}
            ${mode === "custom" ? this._episodeSelect("ed-ep", eps, e.next ?? s.next?.key) : ""}
          </div>
          <div>
            <div class="ed-title">When to watch</div>
            ${this._holdRadios("ed-hold", e.hold ?? s.hold, s)}
            <div class="ed-title vol-title">Volume</div>
            ${this._volumeRadios(s)}
          </div>
        </div>
        <div class="ed-actions">
          <button data-act="move-list" data-id="${s.id}" data-to="${otherList[0]}"><ha-icon icon="mdi:swap-vertical"></ha-icon> ${otherList[1]}</button>
          <span class="spacer"></span>
          <button data-act="cancel">Cancel</button>
          <button class="primary" data-act="save" data-id="${s.id}">Save</button>
        </div>
      </div>`;
  }

  _renderAdd() {
    const a = this._adding;
    const p = a.picked;
    return `
      <div class="addbox">
        <div class="ed-title">Add a show to the ${a.list === "secondary" ? "secondary" : "main"} list</div>
        <form id="search" class="search">
          <input id="q" placeholder="Search for a TV show…" value="${esc(a.query || "")}" autocomplete="off">
          <button class="primary" type="submit" ${a.busy ? "disabled" : ""}>Search</button>
          <button type="button" data-act="add-cancel">Close</button>
        </form>
        ${a.error ? `<div class="error">${esc(a.error)}</div>` : ""}
        ${a.busy && !p ? `<div class="muted">Searching…</div>` : ""}
        ${!p && a.results ? (a.results.length ? `<div class="results">${a.results.map((r, i) => `
          <div class="result ${r.added ? "added" : ""}" data-pick="${i}">
            ${r.poster ? `<img class="poster sm" src="${esc(r.poster)}" alt="">` : `<div class="poster sm ph"></div>`}
            <div class="info">
              <div class="name">${esc(r.title)} ${r.year ? `<span class="muted0">(${r.year})</span>` : ""}</div>
              <div class="line">${r.added ? "Already in your lists" : r.mine.length ? `On ${esc(r.mine.join(", "))}` : `<span class="warnt">Not on your services</span>${r.services.length ? ` · ${esc(r.services.join(", "))}` : ""}`}</div>
            </div>
          </div>`).join("")}</div>` : `<div class="muted">No shows found.</div>`) : ""}
        ${p ? `
          <div class="picked">
            ${p.poster ? `<img class="poster sm" src="${esc(p.poster)}" alt="">` : ""}
            <div class="info"><div class="name">${esc(p.title)}</div>
            <div class="line">${p.mine.length ? `On ${esc(p.mine.join(", "))}` : `<span class="warnt">Not on your services (yet)</span>`}</div></div>
          </div>
          <div class="cols">
            <div>
              <div class="ed-title">Where are you?</div>
              ${this._modeRadios("add-mode", a.mode, a.episodes)}
              ${a.mode === "custom" ? this._episodeSelect("add-ep", a.episodes, a.next) : ""}
            </div>
            <div>
              <div class="ed-title">When to watch</div>
              ${this._holdRadios("add-hold", a.hold, null)}
            </div>
          </div>
          <div class="ed-actions">
            <button data-act="add-back">Back</button>
            <span class="spacer"></span>
            <button class="primary" data-act="add-save" ${a.busy ? "disabled" : ""}>Add to list</button>
          </div>` : ""}
      </div>`;
  }

  // ---------- events ----------
  _bind() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-act]").forEach((el) => el.addEventListener("click", (ev) => this._action(ev, el)));
    root.querySelectorAll("[data-grip]").forEach((g) => g.addEventListener("pointerdown", (ev) => this._dragStart(ev, g)));
    root.querySelectorAll("input[name=ed-mode]").forEach((r) => r.addEventListener("change", () => { this._edit.mode = r.value; this._render(); }));
    root.querySelectorAll("input[name=ed-hold]").forEach((r) => r.addEventListener("change", () => { this._edit.hold = r.value; }));
    root.querySelectorAll("input[name=ed-vol]").forEach((r) => r.addEventListener("change", () => { this._edit.volume_mode = r.value; this._render(); }));
    const edVol = root.getElementById("ed-volume");
    if (edVol) edVol.addEventListener("input", () => {
      this._edit.volume = edVol.value / 100;
      root.getElementById("ed-volume-pct").textContent = `${edVol.value}%`;
    });
    const edEp = root.getElementById("ed-ep");
    if (edEp) edEp.addEventListener("change", () => (this._edit.next = edEp.value));
    const form = root.getElementById("search");
    if (form) {
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        this._search(root.getElementById("q").value);
      });
      const q = root.getElementById("q");
      q.addEventListener("input", () => (this._adding.query = q.value));
      if (!this._adding.picked && !this._adding.results && !this._adding.focused) {
        this._adding.focused = true;
        setTimeout(() => q.focus(), 0);
      }
    }
    root.querySelectorAll("[data-pick]").forEach((el) =>
      el.addEventListener("click", () => this._pick(this._adding.results[+el.dataset.pick]))
    );
    root.querySelectorAll("input[name=add-mode]").forEach((r) => r.addEventListener("change", () => { this._adding.mode = r.value; this._render(); }));
    root.querySelectorAll("input[name=add-hold]").forEach((r) => r.addEventListener("change", () => { this._adding.hold = r.value; }));
    const addEp = root.getElementById("add-ep");
    if (addEp) addEp.addEventListener("change", () => (this._adding.next = addEp.value));
  }

  async _action(ev, el) {
    ev.preventDefault();
    const id = el.dataset.id;
    const act = el.dataset.act;
    try {
      if (act === "play-next") {
        el.disabled = true;
        this._say("Starting…");
        const r = await this._ws({ type: "up_next/play", list: el.dataset.list });
        this._say(r.show ? `Playing ${r.show}` : "Nothing new to play");
      } else if (act === "play") {
        this._busy[id] = true;
        this._render();
        this._say("Starting…");
        await this._ws({ type: "up_next/play", show_id: id });
        this._busy[id] = false;
        this._say("Playing");
      } else if (act === "edit") {
        if (this._editing === id) return this._closeEditor();
        this._editing = id;
        this._edit = {};
        this._render();
        if (!this._episodes[id]) {
          this._episodes[id] = await this._ws({ type: "up_next/episodes", show_id: id });
          this._render();
        }
      } else if (act === "cancel") {
        this._closeEditor();
      } else if (act === "save") {
        const show = this._data.shows.find((s) => s.id === id);
        const e = this._edit;
        const mode = e.mode ?? show.mode;
        if (e.mode || e.next) {
          const next = mode === "custom" ? (e.next ?? show.next?.key ?? this._episodes[id]?.[0]?.key) : null;
          await this._ws({ type: "up_next/set_progress", show_id: id, mode, next });
        }
        if (e.hold && e.hold !== show.hold) {
          await this._ws({ type: "up_next/set_hold", show_id: id, hold: e.hold });
        }
        const vmode = e.volume_mode ?? show.volume_mode;
        if (vmode !== show.volume_mode || (vmode === "fixed" && e.volume != null)) {
          const msg = { type: "up_next/set_volume", show_id: id, mode: vmode };
          if (vmode === "fixed") msg.volume = e.volume ?? show.volume ?? 0.5;
          await this._ws(msg);
        }
        this._closeEditor();
        this._say("Saved");
      } else if (act === "move-list") {
        await this._ws({ type: "up_next/set_list", show_id: id, list: el.dataset.to });
        this._closeEditor();
        this._say(el.dataset.to === "secondary" ? "Moved to the secondary list" : "Moved to the main list");
      } else if (act === "remove") {
        const show = this._data.shows.find((s) => s.id === id);
        if (confirm(`Remove ${show?.title || "this show"} from Up Next?`)) {
          await this._ws({ type: "up_next/remove", show_id: id });
          this._say("Removed");
        }
      } else if (act === "add") {
        const list = el.dataset.list;
        this._adding = this._adding && this._adding.list === list ? null : { list, query: "", mode: "caught_up", hold: "none" };
        this._render();
      } else if (act === "toggle-break") {
        this._showBreak = !this._showBreak;
        try { localStorage.setItem("up_next_show_break", this._showBreak ? "1" : "0"); } catch (e) {}
        this._render();
      } else if (act === "add-cancel") {
        this._adding = null;
        this._render();
      } else if (act === "add-back") {
        this._adding.picked = null;
        this._render();
      } else if (act === "add-save") {
        await this._add();
      }
    } catch (e) {
      this._busy[id] = false;
      this._say(e.message || String(e));
    }
  }

  _closeEditor() {
    this._editing = null;
    this._edit = {};
    this._render();
  }

  async _search(query) {
    if (!query.trim()) return;
    Object.assign(this._adding, { query, busy: true, error: null, results: null, picked: null });
    this._render();
    try {
      this._adding.results = await this._ws({ type: "up_next/search", query });
    } catch (e) {
      this._adding.error = e.message || String(e);
    }
    this._adding.busy = false;
    this._render();
  }

  async _pick(result) {
    if (result.added) return this._say("Already in your lists");
    const a = this._adding;
    Object.assign(a, { picked: result, mode: "caught_up", hold: "none", episodes: null, next: null });
    this._render();
    try {
      a.episodes = await this._ws({ type: "up_next/episodes", jw_id: result.jw_id });
      const firstOut = [...a.episodes].reverse().find((e) => e.out);
      a.next = (firstOut || a.episodes[0])?.key;
    } catch (e) {
      a.episodes = [];
    }
    this._render();
  }

  async _add() {
    const a = this._adding;
    a.busy = true;
    this._render();
    try {
      await this._ws({
        type: "up_next/add",
        jw_id: a.picked.jw_id,
        title: a.picked.title,
        imdb_id: a.picked.imdb_id,
        mode: a.mode,
        next: a.mode === "custom" ? a.next : null,
        list: a.list,
        hold: a.hold,
      });
      this._say(`Added ${a.picked.title}`);
      this._adding = null;
    } catch (e) {
      a.error = e.message || String(e);
      a.busy = false;
    }
    this._render();
  }

  // ---------- drag to reorder (pointer events: mouse + touch) ----------
  // Works within a list and across the two lists.
  _dragStart(ev, grip) {
    ev.preventDefault();
    const card = grip.closest(".card");
    const id = card.dataset.id;
    const fromList = card.parentElement.dataset.list;
    const lists = [...this.shadowRoot.querySelectorAll(".list")];
    const startY = ev.clientY;
    let adjust = 0; // how far the card's slot moved in the layout since the drag began
    this._dragging = true;
    card.classList.add("dragging");
    this.shadowRoot.querySelector(".content").classList.add("is-dragging");

    const place = (y) => {
      // which list is under the pointer (nearest one if between them)
      let target = lists[0];
      let best = Infinity;
      for (const l of lists) {
        const r = l.getBoundingClientRect();
        const dist = y < r.top ? r.top - y : y > r.bottom ? y - r.bottom : 0;
        if (dist < best) {
          best = dist;
          target = l;
        }
      }
      const others = [...target.querySelectorAll(".card:not(.dragging)")];
      const before = others.find((c) => {
        const r = c.getBoundingClientRect();
        return y < r.top + r.height / 2;
      });
      if (before) {
        if (before !== card.nextElementSibling || card.parentElement !== target) target.insertBefore(card, before);
      } else if (card.parentElement !== target || card !== [...target.querySelectorAll(".card")].pop()) {
        const dz = target.querySelector(".dropzone");
        target.insertBefore(card, dz && dz.parentElement === target ? dz : null);
      }
    };
    const move = (e) => {
      const before = card.getBoundingClientRect().top;
      place(e.clientY);
      adjust += card.getBoundingClientRect().top - before; // layout shift from the DOM move
      card.style.transform = `translateY(${e.clientY - startY - adjust}px)`;
    };
    const up = async () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      card.classList.remove("dragging");
      card.style.transform = "";
      this._dragging = false;
      const target = card.parentElement;
      const toList = target.dataset.list;
      const ids = [...target.querySelectorAll(".card")].map((c) => c.dataset.id);
      const idx = ids.indexOf(id);
      const after = idx > 0 ? ids[idx - 1] : null;
      const shown = (s) => this._showBreak || !s.on_break || s.id === id;
      const oldIds = this._data.shows.filter((s) => (s.list || "main") === toList && shown(s)).map((s) => s.id);
      const changed = toList !== fromList || oldIds.indexOf(id) !== idx;
      if (!changed) return this._render();
      // optimistic local update, then tell the backend (hidden shows keep their spot)
      const moved = { ...this._data.shows.find((s) => s.id === id), list: toList };
      const rest = this._data.shows.filter((s) => s.id !== id);
      const main = rest.filter((s) => (s.list || "main") === "main");
      const sec = rest.filter((s) => (s.list || "main") === "secondary");
      const dest = toList === "main" ? main : sec;
      dest.splice(after ? dest.findIndex((s) => s.id === after) + 1 : 0, 0, moved);
      this._data = { ...this._data, shows: [...main, ...sec] };
      this._render();
      try {
        if (toList !== fromList) await this._ws({ type: "up_next/set_list", show_id: id, list: toList });
        await this._ws({ type: "up_next/move", show_id: id, after });
        if (toList !== fromList) this._say(toList === "secondary" ? "Moved to the secondary list" : "Moved to the main list");
      } catch (e) {
        this._say(e.message || String(e));
      }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  }
}

const STYLE = `
  :host { display:block; height:100%; background: var(--primary-background-color); color: var(--primary-text-color);
          font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
  .toolbar { display:flex; align-items:center; height:56px; padding:0 12px; gap:8px;
             background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff);
             border-bottom: 1px solid var(--divider-color); }
  .title { font-size:20px; font-weight:400; margin-left:4px; }
  .content { max-width: 900px; margin: 0 auto; padding: 16px; box-sizing:border-box; }
  .lane { margin-bottom: 28px; }
  button { font: inherit; border-radius: 18px; border: 1px solid var(--divider-color); background: var(--card-background-color);
           color: var(--primary-text-color); padding: 7px 14px; cursor:pointer; display:inline-flex; align-items:center; gap:6px; }
  button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; }
  button:disabled { opacity:.45; cursor: default; }
  button.icon { border:none; background:none; padding:6px; border-radius:50%; color: var(--secondary-text-color); }
  button.icon:not(:disabled):hover, button.icon.on { background: rgba(127,127,127,.15); color: var(--primary-text-color); }
  ha-icon { --mdc-icon-size: 20px; }
  .hero { display:flex; flex-wrap:wrap; gap:12px; align-items:center; justify-content:space-between;
          background: var(--card-background-color); border-radius: 12px; padding: 14px 16px; margin-bottom: 10px;
          box-shadow: var(--ha-card-box-shadow, none); border: 1px solid var(--divider-color); }
  .hero-label { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color: var(--secondary-text-color); }
  .hero-title { font-size:19px; margin-top:2px; }
  .hero-sub { color: var(--secondary-text-color); margin-top:2px; }
  .hero-actions { display:flex; gap:8px; flex-wrap:wrap; }
  .list { display:flex; flex-direction:column; gap:8px; }
  button.link { border:none; background:none; padding:6px 4px; margin-top:6px; color: var(--secondary-text-color); font-size:13px; }
  button.link:hover { color: var(--primary-text-color); }
  .vol-title { margin-top: 14px; }
  input[type=range] { vertical-align: middle; width: 140px; }
  .dropzone { border: 1px dashed var(--divider-color); border-radius: 12px; padding: 18px; text-align: center;
              color: var(--secondary-text-color); font-size: 13px; }
  .is-dragging .dropzone { border-color: var(--primary-color); }
  .card { background: var(--card-background-color); border-radius: 12px; border: 1px solid var(--divider-color);
          box-shadow: var(--ha-card-box-shadow, none); transition: box-shadow .15s; }
  .card.next { border-color: var(--primary-color); }
  .card.held .poster, .card.held .name, .card.held .line { opacity: .6; }
  .card.dragging { box-shadow: 0 8px 24px rgba(0,0,0,.35); z-index: 2; position: relative; transition:none; opacity:.95; }
  .row { display:flex; align-items:center; gap:10px; padding: 8px 8px 8px 4px; }
  .grip { cursor: grab; touch-action: none; padding: 8px 2px; color: var(--secondary-text-color); display:flex; }
  .card.dragging .grip { cursor: grabbing; }
  .rank { width: 18px; text-align:center; color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
  .poster { width: 48px; height: 72px; object-fit: cover; border-radius: 6px; background: rgba(127,127,127,.2); flex: none; }
  .poster.sm { width: 36px; height: 54px; }
  .poster.ph { display:flex; align-items:center; justify-content:center; color: var(--secondary-text-color); }
  .info { flex:1; min-width:0; }
  .name { font-size: 16px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .line { color: var(--primary-text-color); opacity:.85; font-size: 14px; margin-top:2px; }
  .meta { color: var(--secondary-text-color); font-size: 12px; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .actions { display:flex; align-items:center; }
  .badge { font-size: 11px; padding: 1px 8px; border-radius: 10px; background: rgba(127,127,127,.2); color: var(--secondary-text-color); }
  .badge.ok { background: var(--success-color, #43a047); color: #fff; }
  .badge.soon { background: rgba(3,169,244,.18); color: var(--primary-color); }
  .badge.auto { background: rgba(156,39,176,.18); color: #ce93d8; }
  .badge.warn, .warnt { color: var(--warning-color, #ff9800); }
  .badge.warn { background: rgba(255,152,0,.15); }
  .badge.fin { background: rgba(233,30,99,.16); color: #f48fb1; }
  .editor { padding: 4px 16px 14px 16px; border-top: 1px solid var(--divider-color); }
  .addbox { background: var(--card-background-color); border:1px solid var(--divider-color); border-radius: 12px; margin-bottom: 10px; padding: 12px 16px 14px; }
  .cols { display:grid; grid-template-columns: 1fr 1fr; gap: 0 24px; }
  .ed-title { font-weight: 500; margin: 12px 0 6px; }
  .radio { display:flex; align-items:flex-start; gap:8px; padding: 4px 0; cursor:pointer; line-height: 1.35; }
  .radio input { margin-top: 3px; }
  .pv { color: var(--secondary-text-color); }
  .help { color: var(--secondary-text-color); font-size: 12px; }
  select.ep, .search input { width: 100%; box-sizing:border-box; padding: 8px 10px; margin-top: 8px; border-radius: 8px;
           border: 1px solid var(--divider-color); background: var(--primary-background-color); color: var(--primary-text-color); font: inherit; }
  .search { display:flex; gap:8px; align-items:center; margin-top: 4px; }
  .search input { margin-top:0; flex:1; }
  .ed-actions { display:flex; align-items:center; gap:8px; margin-top: 14px; flex-wrap: wrap; }
  .spacer { flex: 1; }
  .results { display:flex; flex-direction:column; margin-top: 8px; }
  .result, .picked { display:flex; gap:10px; align-items:center; padding: 6px; border-radius: 8px; }
  .result { cursor:pointer; }
  .result:hover { background: rgba(127,127,127,.12); }
  .result.added { opacity:.5; cursor: default; }
  .muted { color: var(--secondary-text-color); margin-top: 6px; }
  .muted0 { color: var(--secondary-text-color); }
  .error { color: var(--error-color, #db4437); margin-top: 6px; }
  .empty { color: var(--secondary-text-color); padding: 24px; text-align:center; }
  .toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); background: #323232; color:#fff;
           padding: 10px 18px; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,.3); z-index: 10; }
  @media (max-width: 700px) {
    .content { padding: 8px; }
    .rank { display:none; }
    .meta { white-space: normal; }
    .cols { grid-template-columns: 1fr; }
  }
`;

if (!customElements.get("up-next-panel")) customElements.define("up-next-panel", UpNextPanel);
