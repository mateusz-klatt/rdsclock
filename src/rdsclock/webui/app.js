"use strict";

// Every value comes from the receiver or from broadcasts: it is inserted with
// textContent, never as HTML.

const labels = JSON.parse(document.body.dataset.labels);
const byId = (id) => document.getElementById(id);
const CONSENSUS_FIELDS = [
  "trust",
  "assessed",
  "uncertainty",
  "sources",
  "age",
  "offset",
  "outliers",
];
let paused = false;
let latest = null;

function setText(id, value) {
  byId(id).textContent = value ?? "";
}

function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

function renderConsensus(consensus, note) {
  byId("clock").dataset.trust = consensus ? consensus.trust_level : "";
  setText("note", note);
  if (!consensus) {
    setText("utc", labels.waiting);
    CONSENSUS_FIELDS.forEach((id) => setText(id, ""));
    return;
  }
  setText("trust", consensus.trust_label);
  setText("assessed", consensus.assessed);
  setText("outliers", consensus.outliers);
  if (consensus.utc === null) {
    setText("utc", labels.unavailable);
    setText("uncertainty", consensus.notes);
    ["sources", "age", "offset"].forEach((id) => setText(id, ""));
    return;
  }
  setText("utc", `UTC ${consensus.utc}`);
  setText("uncertainty", consensus.uncertainty);
  setText("sources", consensus.sources);
  setText("age", consensus.age);
  setText("offset", consensus.host_offset);
}

function renderTable(rows) {
  byId("rows").replaceChildren(
    ...rows.map((row) => {
      const tr = document.createElement("tr");
      tr.dataset.status = row.status;
      tr.append(...row.cells.map((value) => element("td", value)), element("td", row.status_label));
      return tr;
    }),
  );
  byId("empty").hidden = rows.length > 0;
}

function renderLog(messages) {
  const log = byId("log");
  const following = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.replaceChildren(...messages.map((message) => element("li", message)));
  if (following) {
    log.scrollTop = log.scrollHeight;
  }
}

function render(view) {
  document.documentElement.lang = view.language;
  setText("status", view.status);
  byId("status").dataset.phase = view.phase;
  renderConsensus(view.consensus, view.note);
  renderTable(view.rows);
  renderLog(view.log);
  byId("rescan").disabled = !view.can_rescan;
}

byId("pause").addEventListener("click", () => {
  paused = !paused;
  const button = byId("pause");
  button.setAttribute("aria-pressed", String(paused));
  button.textContent = paused ? labels.resume : labels.pause;
  if (paused) {
    setText("status", labels.paused);
  } else if (latest) {
    render(latest);
  }
});

byId("rescan").addEventListener("click", async () => {
  try {
    const response = await fetch("api/rescan", {
      method: "POST",
      headers: { "X-Requested-By": "rdsclock" },
    });
    const body = await response.json();
    setText("notice", body.message ?? body.error);
  } catch (error) {
    setText("notice", `${labels.rescan_failed} ${error}`);
  }
});

const events = new EventSource("events");
events.addEventListener("message", (event) => {
  latest = JSON.parse(event.data);
  if (!paused) {
    render(latest);
  }
});
events.addEventListener("error", () => {
  if (!paused) {
    setText("status", labels.disconnected);
  }
});
