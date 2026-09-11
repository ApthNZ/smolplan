"use strict";

const COL = 46; // must match --col in app.css

let state = null;
let view = "portfolio";
let showArchived = false;
let teamFilter = null; // team id, or null for every team

// --- helpers -----------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "style") node.style.cssText = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

const fte = (h) => (h / 100).toFixed(2);
const parseFte = (text) => {
  const value = parseFloat(text);
  return Number.isFinite(value) ? Math.round(value * 100) : null;
};

const mIndex = (m) => {
  const [y, mo] = m.split("-").map(Number);
  return y * 12 + mo - 1;
};
const mFrom = (i) =>
  `${String(Math.floor(i / 12)).padStart(4, "0")}-${String((i % 12) + 1).padStart(2, "0")}`;
const monthAdd = (month, n) => mFrom(mIndex(month) + n);

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function monthLabel(m) {
  const [year, mo] = m.split("-");
  const name = MONTHS[Number(mo) - 1];
  return Number(mo) === 1 ? `${name} ${year.slice(2)}` : name;
}

// Unambiguous form, for dropdowns and tooltips where there is room.
function monthLong(m) {
  const [year, mo] = m.split("-");
  return `${MONTHS[Number(mo) - 1]} ${year}`;
}

function toast(message) {
  const box = $("#toast");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (box.hidden = true), 4000);
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `${method} ${path} failed`);
  }
  return data;
}

async function send(method, path, body) {
  try {
    state = await api(method, path, body);
    render();
  } catch (err) {
    toast(err.message);
  }
}

function drawer(...content) {
  // replaceChildren() stringifies a null into the text "null", unlike el(),
  // which skips it. Drawer sections are routinely conditional, so filter.
  $("#drawer-body").replaceChildren(...content.filter((node) => node != null && node !== false));
  $("#drawer").hidden = false;
}
const closeDrawer = () => ($("#drawer").hidden = true);

const teamName = (id) => (state.teams.find((t) => t.id === id) || {}).name || `Team ${id}`;
const cellAt = (teamId, month) => state.cells[`${teamId}|${month}`];

// Hues chosen to stay clear of the green and red used for status.
const HUES = [210, 265, 320, 30, 190, 250, 340, 160];
const hueFor = (id) => HUES[Math.max(0, state.teams.findIndex((t) => t.id === id)) % HUES.length];
const byTeamOrder = (a, b) =>
  state.teams.findIndex((t) => t.id === a) - state.teams.findIndex((t) => t.id === b);

function teamTag(teamId) {
  return el(
    "span",
    {
      class: `tag ${teamFilter === teamId ? "on" : ""}`,
      style: `--h:${hueFor(teamId)}`,
      title:
        teamFilter === teamId
          ? "Showing this team only. Click to show all."
          : `Show only ${teamName(teamId)}`,
      onclick: (event) => {
        event.stopPropagation();
        teamFilter = teamFilter === teamId ? null : teamId;
        render();
      },
    },
    teamName(teamId)
  );
}

function tagStrip(teamIds) {
  const ids = [...new Set(teamIds)].sort(byTeamOrder);
  const shown = ids.slice(0, 3);
  return el(
    "span",
    { class: "tags", title: ids.map(teamName).join(", ") },
    shown.map(teamTag),
    ids.length > shown.length
      ? el("span", { class: "muted", style: "font-size:10px" }, `+${ids.length - shown.length}`)
      : null
  );
}

const initiativeTeams = (initiative) => initiative.demand.map((d) => d.team_id);
const reserveTeams = (reserve) => Object.keys(reserve.lines).map(Number);
const drawsOnFilteredTeam = (teamIds) => teamFilter === null || teamIds.includes(teamFilter);

// --- portfolio ---------------------------------------------------------------

function renderPortfolio() {
  // A team can be deleted while its filter is active.
  if (teamFilter !== null && !state.teams.some((t) => t.id === teamFilter)) teamFilter = null;

  const months = state.months;
  const width = months.length * COL;
  const visible = state.initiatives.filter(
    (i) => (showArchived || !i.archived) && drawsOnFilteredTeam(initiativeTeams(i))
  );
  const reserves = state.reserves.filter((r) => drawsOnFilteredTeam(reserveTeams(r)));

  const side = el("div", { class: "side" }, el("div", { class: "head" }, "Rank and initiative"));
  const track = el("div", { class: "tl", style: `width:${width}px` });

  track.append(
    el(
      "div",
      { class: "tl-head" },
      months.map((m) =>
        el(
          "div",
          {
            class: [
              Number(m.slice(5)) === 1 ? "q1" : "",
              m === state.settings.current_month ? "now" : "",
            ].join(" "),
            title: m,
          },
          monthLabel(m)
        )
      )
    )
  );

  // Reserves are pinned at the top and cannot be dragged.
  for (const reserve of reserves) {
    side.append(
      el(
        "div",
        { class: "side-row reserve" },
        el("span", { class: "rank" }, "—"),
        el("span", { class: "name" }, reserve.name),
        tagStrip(reserveTeams(reserve))
      )
    );
    const row = el("div", { class: "tl-row" });
    const grid = el("div", { class: "grid" });
    for (const month of months) {
      // With a team filter on, a reserve row shows that team's draw rather
      // than the total across every team.
      let total = 0;
      for (const teamId of Object.keys(reserve.lines)) {
        if (teamFilter !== null && Number(teamId) !== teamFilter) continue;
        total += reserve.lines[teamId][month] || 0;
      }
      grid.append(
        el(
          "i",
          { class: Number(month.slice(5)) === 1 ? "q1" : "" },
          el("span", { class: "reserve-cell" }, total ? fte(total) : "")
        )
      );
    }
    row.append(grid);
    track.append(row);
  }

  for (const initiative of visible) {
    side.append(sideRow(initiative));
    track.append(timelineRow(initiative, months));
  }

  if (!visible.length && !reserves.length) {
    side.append(
      el(
        "div",
        { class: "side-row" },
        el("span", { class: "muted" }, `Nothing draws on ${teamName(teamFilter)}.`)
      )
    );
    track.append(el("div", { class: "tl-row" }));
  }

  const plan = el("div", { class: "plan" }, side, el("div", { class: "tl-scroll" }, track));

  return el(
    "div",
    {},
    el(
      "div",
      { class: "toolbar" },
      el("button", { class: "primary", onclick: () => openEditor(null) }, "New initiative"),
      el(
        "label",
        { style: "display:flex;gap:5px;align-items:center;margin:0" },
        el("input", {
          type: "checkbox",
          ...(showArchived ? { checked: true } : {}),
          onchange: (e) => {
            showArchived = e.target.checked;
            render();
          },
        }),
        "Show archived"
      ),
      el(
        "span",
        {
          class: "row",
          style: "gap:5px;align-items:center;border-left:1px solid var(--line);padding-left:12px",
        },
        el("span", { class: "muted" }, "Team"),
        el(
          "select",
          {
            title: "Show only rows that draw on one team",
            onchange: (e) => {
              teamFilter = e.target.value ? Number(e.target.value) : null;
              render();
            },
          },
          el("option", teamFilter === null ? { value: "", selected: true } : { value: "" }, "All teams"),
          state.teams.map((t) =>
            el("option", teamFilter === t.id ? { value: t.id, selected: true } : { value: t.id }, t.name)
          )
        )
      ),
      el("span", { class: "spacer" }),
      el("span", { class: "muted" }, "Drag a row to re-rank. Drag a bar to move the start.")
    ),
    plan,
    el(
      "div",
      { class: "legend" },
      el("span", {}, "● Green: fully staffed"),
      el("span", {}, "▲ Red: cannot be staffed, consumes nothing"),
      el("span", {}, "Grey: in the past or archived")
    )
  );
}

function sideRow(initiative) {
  const row = el(
    "div",
    {
      class: `side-row ${initiative.archived ? "archived" : ""}`,
      draggable: "true",
      ondragstart: (e) => {
        e.dataTransfer.setData("text/plain", String(initiative.id));
        e.dataTransfer.effectAllowed = "move";
      },
      ondragover: (e) => {
        e.preventDefault();
        const box = row.getBoundingClientRect();
        const below = e.clientY > box.top + box.height / 2;
        row.classList.toggle("drop-above", !below);
        row.classList.toggle("drop-below", below);
      },
      ondragleave: () => row.classList.remove("drop-above", "drop-below"),
      ondrop: (e) => {
        e.preventDefault();
        row.classList.remove("drop-above", "drop-below");
        const dragged = Number(e.dataTransfer.getData("text/plain"));
        if (!dragged || dragged === initiative.id) return;
        const box = row.getBoundingClientRect();
        const below = e.clientY > box.top + box.height / 2;
        const order = state.initiatives.map((i) => i.id).filter((id) => id !== dragged);
        const at = order.indexOf(initiative.id) + (below ? 1 : 0);
        order.splice(at, 0, dragged);
        send("POST", "/api/initiatives/reorder", { ordered_ids: order });
      },
    },
    el("span", { class: "rank" }, initiative.rank),
    el("span", { class: `dot ${initiative.status}` }),
    el(
      "span",
      { class: "name" },
      el("button", { onclick: () => openEditor(initiative), title: "Edit" }, initiative.name)
    ),
    tagStrip(initiativeTeams(initiative))
  );
  return row;
}

function timelineRow(initiative, months) {
  const row = el("div", { class: "tl-row" });
  const grid = el("div", { class: "grid" });
  for (const month of months) {
    grid.append(el("i", { class: Number(month.slice(5)) === 1 ? "q1" : "" }));
  }
  row.append(grid);

  const first = mIndex(months[0]);
  const offset = mIndex(initiative.start_month) - first;
  const span = initiative.length;
  const left = Math.max(0, offset) * COL;
  const cells = Math.min(span + Math.min(0, offset), months.length - Math.max(0, offset));

  if (cells <= 0) {
    row.append(
      el(
        "div",
        { class: "bar past", style: `left:0;width:${COL * 2}px`, title: "Ended before the current month" },
        "ended"
      )
    );
    return row;
  }

  const locked = initiative.archived;
  const bar = el(
    "div",
    {
      class: `bar ${initiative.status} ${locked ? "locked" : ""}`,
      style: `left:${left}px;width:${cells * COL - 4}px`,
      title: locked
        ? `${initiative.name} — archived`
        : `${initiative.name} — drag to move the start`,
    },
    initiative.status === "red" ? el("span", { class: "icon" }, "▲") : null,
    el("span", {}, initiative.name)
  );

  bar.addEventListener("pointerdown", (event) => startBarDrag(event, initiative, bar, grid, months, locked));
  row.append(bar);
  return row;
}

function startBarDrag(event, initiative, bar, grid, months, locked) {
  event.preventDefault();
  const startX = event.clientX;
  // A start behind the current month is drawn clipped at column zero, so the
  // drag is measured from where the bar actually sits.
  const origin = Math.max(0, mIndex(initiative.start_month) - mIndex(months[0]));
  let target = origin;
  let moved = false;

  if (!locked) {
    api("GET", `/api/initiatives/${initiative.id}/fit`)
      .then(({ hints }) => {
        const set = new Set(hints);
        [...grid.children].forEach((cell, index) => {
          cell.classList.toggle("hint", set.has(months[index]));
        });
      })
      .catch(() => {});
  }

  const onMove = (e) => {
    if (locked) return;
    const delta = Math.round((e.clientX - startX) / COL);
    if (delta === 0 && !moved) return;
    moved = true;
    bar.classList.add("dragging");
    target = Math.min(Math.max(origin + delta, 0), months.length - 1);
    bar.style.left = `${target * COL}px`;
  };

  const onUp = () => {
    bar.releasePointerCapture(event.pointerId);
    bar.removeEventListener("pointermove", onMove);
    bar.removeEventListener("pointerup", onUp);
    bar.classList.remove("dragging");

    if (!moved || months[target] === initiative.start_month) {
      bar.style.left = `${origin * COL}px`;
      [...grid.children].forEach((cell) => cell.classList.remove("hint"));
      openShortfalls(initiative);
      return;
    }
    send("PATCH", `/api/initiatives/${initiative.id}`, { start_month: months[target] });
  };

  bar.setPointerCapture(event.pointerId);
  bar.addEventListener("pointermove", onMove);
  bar.addEventListener("pointerup", onUp);
}

function openShortfalls(initiative) {
  const rows = initiative.shortfalls.map((s) =>
    el(
      "li",
      {},
      `${teamName(s.team_id)} ${s.month}: short ${fte(s.short)} `,
      el("span", { class: "muted" }, s.reason === "no_supply_data" ? "(no supply data)" : "")
    )
  );
  drawer(
    el("h2", {}, initiative.name),
    el(
      "dl",
      {},
      el("dt", {}, "Status"),
      el("dd", { class: `status-${initiative.status}` }, initiative.status),
      el("dt", {}, "Rank"),
      el("dd", {}, initiative.rank),
      el("dt", {}, "Start"),
      el("dd", {}, initiative.start_month)
    ),
    rows.length ? el("h3", {}, "Shortfalls") : null,
    rows.length ? el("ul", {}, rows) : el("p", { class: "muted" }, "No shortfalls."),
    el("button", { onclick: () => openEditor(initiative) }, "Edit initiative")
  );
}

// --- initiative editor -------------------------------------------------------

function openEditor(initiative) {
  const isNew = !initiative;
  const months = state.months;
  const data = initiative || {
    name: "",
    owner: "",
    notes: "",
    start_month: months[0],
    demand: [],
    length: 1,
    archived: false,
  };

  let columns = Math.min(24, Math.max(6, data.length + 2));
  const grid = new Map(data.demand.map((d) => [`${d.team_id}|${d.offset}`, d.fte_h]));

  const nameInput = el("input", { value: data.name, placeholder: "Initiative name" });
  const ownerInput = el("input", { value: data.owner || "" });
  const notesInput = el("input", { value: data.notes || "" });
  const startSelect = el(
    "select",
    {},
    months.map((m) => el("option", m === data.start_month ? { value: m, selected: true } : { value: m }, m))
  );
  // A start behind the current month is outside the display range. Keep it as
  // an option so saving the other fields does not silently move it forward.
  if (!months.includes(data.start_month)) {
    startSelect.prepend(el("option", { value: data.start_month, selected: true }, data.start_month));
  }

  const gridBox = el("div", { class: "scroll-x" });

  // Demand is stored as an offset from the start month, so that moving the
  // start moves the whole profile. The grid shows the months those offsets
  // currently land on, because "+3" means nothing to a reader.
  const columnMonth = (offset) => monthAdd(startSelect.value, offset);

  function drawGrid() {
    const head = el(
      "tr",
      {},
      el("th", { class: "team" }, "Team"),
      Array.from({ length: columns }, (_, offset) =>
        el(
          "th",
          { title: `${monthLong(columnMonth(offset))} — month ${offset + 1} of the initiative` },
          monthLabel(columnMonth(offset))
        )
      )
    );
    const body = state.teams.map((team) =>
      el(
        "tr",
        {},
        el("td", { class: "team" }, team.name),
        Array.from({ length: columns }, (_, offset) => {
          const key = `${team.id}|${offset}`;
          return el(
            "td",
            { class: "grid-in" },
            el("input", {
              type: "number",
              step: "0.05",
              min: "0",
              value: grid.has(key) ? fte(grid.get(key)) : "",
              onchange: (e) => {
                const value = parseFte(e.target.value);
                if (value == null || value === 0) grid.delete(key);
                else grid.set(key, value);
              },
            })
          );
        })
      )
    );
    gridBox.replaceChildren(el("table", {}, el("thead", {}, head), el("tbody", {}, body)));
  }
  drawGrid();

  const fillTeam = el("select", {}, state.teams.map((t) => el("option", { value: t.id }, t.name)));
  const fillFrom = el("select", {});
  const fillTo = el("select", {});
  const fillValue = el("input", { type: "number", step: "0.05", min: "0", value: "0.50", style: "width:62px" });

  // Rebuilt whenever the start month or the column count changes, so the
  // dropdowns always name the months actually on screen.
  function drawFillRange() {
    const from = Number(fillFrom.value || 0);
    const to = fillTo.value === "" ? columns - 1 : Number(fillTo.value);
    const options = (offset) =>
      Array.from({ length: columns }, (_, o) =>
        el("option", o === offset ? { value: o, selected: true } : { value: o }, monthLong(columnMonth(o)))
      );
    fillFrom.replaceChildren(...options(Math.min(from, columns - 1)));
    fillTo.replaceChildren(...options(Math.min(to, columns - 1)));
  }
  drawFillRange();

  startSelect.addEventListener("change", () => {
    drawGrid();
    drawFillRange();
  });

  async function save() {
    const lines = [...grid.entries()].map(([key, value]) => {
      const [teamId, offset] = key.split("|").map(Number);
      return { team_id: teamId, offset, fte_h: value };
    });
    try {
      let id = initiative && initiative.id;
      if (isNew) {
        const created = await api("POST", "/api/initiatives", {
          name: nameInput.value.trim() || "Untitled",
          start_month: startSelect.value,
          owner: ownerInput.value,
          notes: notesInput.value,
        });
        state = created;
        id = Math.max(...state.initiatives.map((i) => i.id));
      } else {
        const patch = {
          name: nameInput.value.trim() || data.name,
          owner: ownerInput.value,
          notes: notesInput.value,
        };
        patch.start_month = startSelect.value;
        state = await api("PATCH", `/api/initiatives/${id}`, patch);
      }
      state = await api("PUT", `/api/initiatives/${id}/demand`, { lines });
      render();
      closeDrawer();
    } catch (err) {
      toast(err.message);
    }
  }

  drawer(
    el("h2", {}, isNew ? "New initiative" : `Edit ${data.name}`),
    field("Name", nameInput),
    field("Owner", ownerInput),
    field("Start month", startSelect),
    field("Notes", notesInput),
    el("h3", {}, "Demand, FTE per team per month"),
    el(
      "p",
      { class: "muted", style: "margin:0 0 6px" },
      "Months follow the start month. Move the initiative and the whole profile moves with it."
    ),
    gridBox,
    el(
      "div",
      { style: "margin-top:8px" },
      el(
        "div",
        { class: "row" },
        el("span", { class: "muted" }, "Set"),
        fillTeam,
        el("span", { class: "muted" }, "to"),
        fillValue,
        el("span", { class: "muted" }, "FTE")
      ),
      el(
        "div",
        { class: "row", style: "margin-top:5px" },
        el("span", { class: "muted" }, "from"),
        fillFrom,
        el("span", { class: "muted" }, "to"),
        fillTo,
        el(
        "button",
        {
          onclick: () => {
            const value = parseFte(fillValue.value);
            const from = Number(fillFrom.value);
            const to = Number(fillTo.value);
            for (let offset = from; offset <= to && offset < columns; offset++) {
              const key = `${fillTeam.value}|${offset}`;
              if (!value) grid.delete(key);
              else grid.set(key, value);
            }
            drawGrid();
          },
        },
          "Apply"
        ),
        el(
          "button",
          {
            onclick: () => {
              columns = Math.min(24, columns + 3);
              drawGrid();
              drawFillRange();
            },
          },
          "+3 months"
        )
      )
    ),
    el(
      "div",
      { class: "row", style: "margin-top:16px" },
      el("button", { class: "primary", onclick: save }, isNew ? "Create initiative" : "Save changes"),
      !isNew
        ? el(
            "button",
            {
              onclick: () =>
                send("PATCH", `/api/initiatives/${initiative.id}`, { archived: !data.archived }).then(
                  closeDrawer
                ),
            },
            data.archived ? "Restore" : "Archive"
          )
        : null,
      !isNew
        ? el(
            "button",
            {
              class: "danger",
              onclick: () => {
                if (confirm(`Delete ${data.name}? This cannot be undone.`)) {
                  send("DELETE", `/api/initiatives/${initiative.id}`).then(closeDrawer);
                }
              },
            },
            "Delete"
          )
        : null
    ),
    !isNew ? el("p", { class: "muted", style: "margin-top:12px" }, `Status: ${data.status}`) : null
  );
}

function field(label, input) {
  return el("div", { class: "field" }, el("label", {}, label), input);
}

// --- capacity heatmap --------------------------------------------------------

function renderHeatmap() {
  const months = state.months;
  const head = el(
    "tr",
    {},
    el("th", { class: "team" }, "Team"),
    months.map((m) => el("th", {}, monthLabel(m)))
  );

  const rows = state.teams.map((team) =>
    el(
      "tr",
      {},
      el("td", { class: "team" }, team.name),
      months.map((month) => {
        const cell = cellAt(team.id, month);
        if (!cell) return el("td", {}, "");
        const used = cell.reserved + cell.allocated;
        const pct = cell.supply ? Math.round((used / cell.supply) * 100) : used ? 999 : 0;
        const classes = ["cell"];
        if (cell.warnings.length) classes.push("warn");
        if (!cell.has_supply_row) classes.push("nodata");
        return el(
          "td",
          {
            class: classes.join(" "),
            title: `${team.name} ${month}`,
            onclick: () => openCell(team, month),
          },
          el("span", {}, cell.has_supply_row ? `${pct}%` : "—"),
          el("span", { class: "free" }, `${fte(cell.free)} free`)
        );
      })
    )
  );

  return el(
    "div",
    {},
    el(
      "div",
      { class: "toolbar" },
      el("span", { class: "muted" }, "Utilisation of supply, and free FTE after reserves and green initiatives. Click a cell for the breakdown.")
    ),
    el("div", { class: "scroll-x" }, el("table", {}, el("thead", {}, head), el("tbody", {}, rows)))
  );
}

function openCell(team, month) {
  const cell = cellAt(team.id, month);
  const consumers = cell.consumers.map((c) =>
    el("li", {}, `${c.name} — ${fte(c.fte_h)} `, el("span", { class: "muted" }, c.kind))
  );
  drawer(
    el("h2", {}, `${team.name}, ${month}`),
    el(
      "dl",
      {},
      el("dt", {}, "Supply"),
      el("dd", {}, cell.has_supply_row ? fte(cell.supply) : "No supply data"),
      el("dt", {}, "Reserved"),
      el("dd", {}, fte(cell.reserved)),
      el("dt", {}, "Allocated"),
      el("dd", {}, fte(cell.allocated)),
      el("dt", {}, "Free"),
      el("dd", {}, fte(cell.free))
    ),
    cell.warnings.includes("reserve_exceeds_supply")
      ? el("p", { class: "status-red" }, "Reserves exceed supply here. Available capacity is clamped to zero.")
      : null,
    consumers.length ? el("h3", {}, "Consuming this cell") : null,
    consumers.length ? el("ul", {}, consumers) : el("p", { class: "muted" }, "Nothing allocated."),
    el(
      "p",
      { class: "muted" },
      "Red initiatives are not listed: they consume nothing until they fit."
    )
  );
}

// --- supply and reserves -----------------------------------------------------

function renderSupply() {
  return el(
    "div",
    {},
    el("h2", {}, "Teams"),
    el(
      "div",
      { class: "row", style: "margin-bottom:14px" },
      state.teams.map((team) =>
        el(
          "span",
          { class: "row", style: "gap:2px" },
          el("input", {
            value: team.name,
            style: "width:110px",
            onchange: (e) => send("PATCH", `/api/teams/${team.id}`, { name: e.target.value }),
          }),
          el(
            "button",
            {
              class: "danger",
              title: "Delete team",
              onclick: () =>
                confirm(`Delete ${team.name}, its supply, reserves and demand?`) &&
                send("DELETE", `/api/teams/${team.id}`),
            },
            "×"
          )
        )
      ),
      el(
        "button",
        {
          onclick: () => {
            const name = prompt("Team name");
            if (name) send("POST", "/api/teams", { name });
          },
        },
        "Add team"
      )
    ),

    el("h2", {}, "Supply"),
    fillForm(
      "Set supply",
      "Leave the FTE box empty to clear those months. A cleared month has no " +
        "supply data, which is not the same as a supply of zero: shortfalls " +
        "there are labelled separately.",
      (payload) => send("PUT", "/api/supply", payload)
    ),
    monthGrid(
      (team, month) => (state.supply[String(team.id)] || {})[month],
      (team, month, value) =>
        send("PUT", "/api/supply", {
          team_id: team.id,
          from_month: month,
          to_month: month,
          fte_h: value,
        })
    ),

    ...state.reserves.map((reserve) =>
      el(
        "div",
        {},
        el(
          "h2",
          { style: "margin-top:20px" },
          `Reserve: ${reserve.name} `,
          el(
            "button",
            {
              class: "danger",
              style: "margin-left:6px",
              onclick: () =>
                confirm(`Delete the ${reserve.name} reserve?`) &&
                send("DELETE", `/api/reserves/${reserve.id}`),
            },
            "Delete"
          )
        ),
        fillForm(
          "Set reserve",
          "Leave the FTE box empty, or set it to zero, to remove this reserve " +
            "from those months.",
          (payload) => send("PUT", `/api/reserves/${reserve.id}/lines`, payload)
        ),
        monthGrid(
          (team, month) => (reserve.lines[String(team.id)] || {})[month],
          (team, month, value) =>
            send("PUT", `/api/reserves/${reserve.id}/lines`, {
              team_id: team.id,
              from_month: month,
              to_month: month,
              fte_h: value,
            })
        )
      )
    ),

    el(
      "div",
      { style: "margin-top:16px" },
      el(
        "button",
        {
          onclick: () => {
            const name = prompt("Reserve name");
            if (name) send("POST", "/api/reserves", { name });
          },
        },
        "Add reserve"
      )
    )
  );
}

function fillForm(label, hint, submit) {
  const team = el("select", {}, state.teams.map((t) => el("option", { value: t.id }, t.name)));
  const from = el("input", { value: state.months[0], style: "width:88px" });
  const to = el("input", { value: state.months[state.months.length - 1], style: "width:88px" });
  const value = el("input", { type: "number", step: "0.05", min: "0" });
  return el(
    "div",
    { style: "margin-bottom:12px" },
    el(
      "div",
      { class: "row" },
      el("span", { class: "muted" }, "Set"),
      team,
      el("span", { class: "muted" }, "to"),
      value,
      el("span", { class: "muted" }, "FTE from"),
      from,
      el("span", { class: "muted" }, "to"),
      to,
      el(
        "button",
        {
          onclick: () =>
            submit({
              team_id: Number(team.value),
              from_month: from.value.trim(),
              to_month: to.value.trim(),
              fte_h: value.value.trim() === "" ? null : parseFte(value.value),
            }),
        },
        label
      )
    ),
    el("p", { class: "muted", style: "margin:5px 0 0" }, hint)
  );
}

function monthGrid(read, write) {
  const months = state.months;
  const head = el(
    "tr",
    {},
    el("th", { class: "team" }, "Team"),
    months.map((m) => el("th", {}, monthLabel(m)))
  );
  const rows = state.teams.map((team) =>
    el(
      "tr",
      {},
      el("td", { class: "team" }, team.name),
      months.map((month) => {
        const current = read(team, month);
        return el(
          "td",
          { class: "grid-in" },
          el("input", {
            type: "number",
            step: "0.05",
            min: "0",
            value: current == null ? "" : fte(current),
            title: `${team.name} ${month}`,
            onchange: (e) =>
              write(team, month, e.target.value.trim() === "" ? null : parseFte(e.target.value)),
          })
        );
      })
    )
  );
  return el("div", { class: "scroll-x" }, el("table", {}, el("thead", {}, head), el("tbody", {}, rows)));
}

// --- settings ----------------------------------------------------------------

function renderSettings() {
  const horizon = el("input", { type: "number", min: "1", max: "120", value: state.settings.horizon_months });
  const clock = el("input", {
    value: state.settings.current_month_override ? state.settings.current_month : "",
    placeholder: state.settings.today_month,
    style: "width:100px",
  });

  return el(
    "div",
    { style: "max-width:520px" },
    el("h2", {}, "Settings"),
    field("Horizon, months (display range only)", horizon),
    el(
      "div",
      { class: "field" },
      el("label", {}, "Current month override (blank uses the real clock)"),
      el(
        "div",
        { class: "row" },
        clock,
        el("span", { class: "muted" }, `today is ${state.settings.today_month}`)
      )
    ),
    el(
      "div",
      { class: "row" },
      el(
        "button",
        {
          class: "primary",
          onclick: () =>
            send("PUT", "/api/settings", {
              horizon_months: Number(horizon.value),
              current_month: clock.value.trim(),
            }),
        },
        "Save settings"
      )
    ),
    el("h3", {}, "Fixture"),
    el(
      "p",
      { class: "muted" },
      "Reset everything to the demo fixture: teams SOC and GRC, and initiatives " +
        "A, B and C laid out from the current month, with B short of GRC."
    ),
    el(
      "button",
      {
        class: "danger",
        onclick: () =>
          confirm("Delete all data and reload the fixture?") && send("POST", "/api/seed"),
      },
      "Reset to fixture"
    )
  );
}

// --- rules -------------------------------------------------------------------

const RULES = [
  ["R1", "Plan in whole months, and store FTE as integer hundredths.",
   "0.50 FTE is held as 50. Ten hundredths plus twenty hundredths is exactly thirty, where 0.1 + 0.2 in floating point is not."],
  ["R2", "Reserves are taken before any initiative.",
   "Business as usual and unplanned work come off the top, so what initiatives compete for is what is genuinely left."],
  ["R3", "Initiatives are allocated in strict rank order.",
   "Rank is the only priority signal. There is no scoring, no weighting, and nothing schedules itself."],
  ["R4", "All or nothing.",
   "An initiative is green only if every month it needs is fully met, for every team it needs. Otherwise it is red."],
  ["R5", "Higher rank always wins.",
   "Adding or re-ranking an initiative can turn a lower-ranked one red, including work already under way. That displacement is what the tool exists to show."],
  ["R6", "There is no pausing. Work that stops is split.",
   "Shorten the initiative to the last month delivered and create a new one for the remainder. A gap in the middle would hide the fact that it stopped."],
  ["R7", "Only the current and future months are evaluated.",
   "The past is settled. An initiative lying entirely behind the current month can never turn red."],
  ["R8", "A start cannot be moved into the past.",
   "Anything can be pushed out, including work that has already begun. Nothing can be dragged behind the clock."],
  ["R9", "A month with no supply figure counts as zero, but is labelled differently.",
   "\u201cNo supply data\u201d means nobody has said what that team has. That is not the same as saying they have none, and a shortfall tells you which it is."],
  ["R10", "Reserves may exceed supply.",
   "The cell is flagged and available capacity clamps to zero. Negative capacity is not a thing."],
];

const SURPRISES = [
  ["A red initiative consumes nothing at all.",
   "It is not half-started and quietly holding capacity. Lower-ranked work fits around it, so the plan shows what would actually be delivered rather than a queue."],
  ["Re-ranking can turn green work red.",
   "That is the point. The cost of a new priority is made visible rather than absorbed silently."],
  ["Demand travels with the start month.",
   "The demand grid is relative to the start, so moving an initiative moves its whole profile with it."],
  ["The months that shade while you drag ignore lower-ranked work.",
   "They show where this initiative would be green given everything ranked above it. What it would displace below only appears once you drop it."],
  ["Nothing is scheduled automatically.",
   "smolplan shows where things fit and what breaks. A human decides."],
];

function renderRules() {
  const status = (cls, label, text) =>
    [el("span", { class: `chip ${cls}` }, cls === "red" ? "\u25b2 " + label : label),
     el("span", { class: "muted" }, text)];

  return el(
    "div",
    { class: "prose" },
    el("h2", {}, "What this is"),
    el("p", { class: "lead" },
      "smolplan answers three questions about a portfolio: what can be delivered, " +
      "when it can be delivered, and what gets displaced when priorities change. " +
      "It plans in whole months, against teams rather than named people."),

    el("h2", {}, "How a plan is worked out"),
    el("ol", {},
      el("li", {}, "Each team has an FTE supply for each month, set under ",
        el("b", {}, "Supply and reserves"), "."),
      el("li", {}, "Reserves come off the top before anything else is considered."),
      el("li", {}, "Initiatives are allocated in rank order, starting from rank 1."),
      el("li", {}, "Any initiative that cannot be ", el("b", {}, "fully"),
        " staffed turns red and consumes nothing."),
      el("li", {}, "You drag it to a month where it fits, or change what sits above it.")),

    el("h2", {}, "What the colours mean"),
    el("div", { class: "statuses" },
      status("green", "Green", "Fully staffed in every evaluated month, for every team it needs."),
      status("red", "Red", "Short in at least one month. Consumes no capacity anywhere until it fits."),
      status("past", "Past", "Lies entirely behind the current month, so it is not evaluated."),
      status("archived", "Archived", "Set aside. Excluded from the plan entirely.")),
    el("p", { class: "muted" },
      "Red bars are hatched and carry a \u25b2 as well as being red, so status never depends on colour alone."),

    el("h2", {}, "The rules"),
    el("table", { class: "rules" },
      el("tbody", {}, RULES.map(([id, what, why]) =>
        el("tr", {},
          el("td", { class: "id" }, id),
          el("td", {}, el("b", {}, what), el("span", { class: "why" }, why)))))),

    el("h2", {}, "Things that catch people out"),
    el("ul", {}, SURPRISES.map(([what, why]) =>
      el("li", {}, el("b", {}, what), " ", el("span", { class: "muted" }, why)))),

    el("h2", {}, "Two practical notes"),
    el("p", {},
      el("b", {}, "The current month drives everything. "),
      el("span", { class: "muted" },
        "It comes from the clock, and Settings has an override so you can push time " +
        "around and watch the rules behave. An override is flagged in the header \u2014 " +
        "blank it to go back to the real date.")),
    el("p", {},
      el("b", {}, "There is no login. "),
      el("span", { class: "muted" },
        "Anyone who can open this page can change the plan, and changes are not " +
        "attributed to anyone. Treat it as a shared whiteboard.")),
  );
}

// --- shell -------------------------------------------------------------------

function render() {
  const views = {
    portfolio: renderPortfolio,
    heatmap: renderHeatmap,
    supply: renderSupply,
    rules: renderRules,
    settings: renderSettings,
  };
  $("#view").replaceChildren(views[view]());
  for (const button of document.querySelectorAll("#tabs button")) {
    button.classList.toggle("on", button.dataset.view === view);
  }
  const settings = state.settings;
  $("#clock").replaceChildren(
    el("span", {}, "Current month "),
    el("b", { class: settings.current_month_override ? "fake" : "" }, settings.current_month),
    el("span", {}, settings.current_month_override ? " (override)" : "")
  );
}

document.querySelectorAll("#tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    view = button.dataset.view;
    closeDrawer();
    render();
  });
});
$("#drawer-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => e.key === "Escape" && closeDrawer());

api("GET", "/api/state")
  .then((data) => {
    state = data;
    render();
  })
  .catch((err) => toast(err.message));
