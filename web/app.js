const $ = (selector) => document.querySelector(selector);
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const time = (value, seconds = false) =>
  value
    ? new Date(value).toLocaleTimeString("pl-PL", {
        timeZone: "Europe/Warsaw",
        hour: "2-digit",
        minute: "2-digit",
        ...(seconds ? { second: "2-digit" } : {}),
      })
    : "—";
const day = (value) =>
  new Date(value).toLocaleDateString("pl-PL", { timeZone: "Europe/Warsaw" });
const dateLabel = (value) =>
  new Date(value).toLocaleDateString("pl-PL", {
    timeZone: "Europe/Warsaw",
    day: "numeric",
    month: "long",
  });
const catName = id => ({kefir: "Kefir", kalinka: "Kalinka", unknown: "Kot nierozpoznany"}[id] || "Kot nierozpoznany");
const trayLabel = v => (v.box_ids?.length > 1 ? "Kuwety " + v.box_ids.map(id => `0${id}`).join(" → ") : `Kuweta 0${v.box_id}`);
const catShort = id => ({kefir: "Kf", kalinka: "Kl"}[id] || "?");
const visibleVisits = () => state.visits.filter(v => v.source !== "mock" || $("#show-mock").checked);
let state = null;
let lastNotice = null;
let connected = false;
let selectedVisit = null;
let selectedCat = null;
let serverOffset = 0;
let toastTimer;

function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    $("#toast").hidden = true;
  }, 4500);
}
async function post(path, body = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(data.error || "Nie udało się zapisać danych.");
  return data;
}
function duration(visit) {
  const seconds = Math.max(
    0,
    Math.round(
      ((visit.exited_at
        ? Date.parse(visit.exited_at)
        : Date.now() + serverOffset) -
        Date.parse(visit.entered_at)) /
        1000,
    ),
  );
  return seconds < 60
    ? `${seconds} s`
    : `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
}
function catDrawing(cat) {
  const kalinka = cat === "kalinka";
  return `<svg viewBox="0 0 64 64" aria-hidden="true"><path d="M${kalinka ? "19" : "14"} 52c-4-12 0-24 4-29L20 9l13 9 12-8 1 16c8 11 9 22 1 28Z" fill="${kalinka ? "#c69b79" : "#839077"}"/><path d="M23 33c5-9 16-9 21 0v11c-3 12-20 12-21 0Z" fill="${kalinka ? "#f1dcc3" : "#e5e7d6"}"/><path d="m24 15 6 8-7 2m17-6-5 6 7-2" fill="${kalinka ? "#936848" : "#53664d"}"/><circle cx="28" cy="36" r="1.7" fill="#344332"/><circle cx="40" cy="36" r="1.7" fill="#344332"/><path d="m32 42 2 2 2-2m-2 2v3" fill="none" stroke="#6d765d" stroke-width="1.5"/>${kalinka ? '<path d="M46 51c11-4 14-14 9-17" fill="none" stroke="#8f674a" stroke-width="5" stroke-linecap="round"/>' : ""}</svg>`;
}
const urineOutcomes = ["urine", "both", "confirmed", "both_confirmed"];
const fecesOutcomes = ["feces", "both", "feces_confirmed", "both_confirmed"];
const confirmedUrine = ["confirmed", "both_confirmed"];
function wasteMarks(visits) {
  return visits.filter(v => v.status === "done").flatMap(v => [
    ...(urineOutcomes.includes(v.outcome) && v.region != null ? [{...v, point: v.urine_point, kind: "Mocz"}] : []),
    ...(fecesOutcomes.includes(v.outcome) && v.feces_region != null ? [{...v, region: v.feces_region, point: v.feces_point, kind: "Kał"}] : []),
  ]);
}
function proposedMarks(visits) {
  return visits.filter(v => v.source === "camera" && v.status === "done" && v.outcome === "uncertain" && !v.reviewed_at).flatMap(v => {
    const [key, index] = v.id.split("-");
    const recording = state.recordings?.items.find(r => r.media_key === key);
    if (!recording?.analysis) return [];
    try {
      const suggestions = JSON.parse(recording.analysis).visits?.[Number(index)]?.focus?.suggestions || [];
      const regions = [...new Set(suggestions.map(s => s.region))].filter(r => Number.isInteger(r) && r >= 0 && r < state.regions.length);
      return regions.map(region => ({...v, region}));
    } catch {
      return [];
    }
  });
}
function proposalLabel(visit) {
  const proposals = proposedMarks([visit]);
  return proposals.length ? `<span class="table-sub proposal-label">P · ${proposals.map(p => escape(state.regions[p.region])).join("; ")} · do oceny</span>` : "";
}
function locationLabel(v, kind) {
  const urine = kind === "urine";
  if (!(urine ? urineOutcomes : fecesOutcomes).includes(v.outcome)) return "brak oznaczenia";
  const region = urine ? v.region : v.feces_region;
  return region == null ? "nieustalone" : escape(state.regions[region]);
}
function renderCats() {
  $("#cat-cards").innerHTML = state.cats
    .map((cat) => {
      const visits = visibleVisits().filter((v) => v.cat_id === cat.id);
      const today = visits.filter((v) => day(v.entered_at) === day(new Date()));
      const active = visits.find((v) => v.status === "active");
      const last = visits.find((v) =>
        urineOutcomes.includes(v.outcome),
      );
      const probable = today.filter((v) => ["urine", "both"].includes(v.outcome)).length;
      const confirmed = today.filter((v) => confirmedUrine.includes(v.outcome)).length;
      const feces = today.filter(v => fecesOutcomes.includes(v.outcome)).length;
      return `<a class="cat-card" href="#cat-${cat.id}" data-cat-profile="${cat.id}" aria-label="Profil kota ${escape(cat.name)} — historia wizyt"><div class="avatar ${cat.id}">${catDrawing(cat.id)}</div><div><div class="cat-top"><h2>${escape(cat.name)}</h2><span class="status-pill ${active ? "active" : ""}">${active ? `W kuwecie ${active.box_id}` : "Profil i historia ↗"}</span></div><p class="cat-sub">${escape(cat.description)}</p><div class="cat-metrics"><div class="metric"><strong>${today.length}</strong><span>wizyt dzisiaj</span></div><div class="metric"><strong>${probable} <small>/ ${confirmed}</small></strong><span>mocz: prawdop. / potw.</span><small>kał: ${feces} wizyt (łącznie)</small></div><div class="metric last"><strong>${last ? time(last.exited_at) : "—"}</strong><span>ostatni ${confirmedUrine.includes(last?.outcome) ? "potwierdzony" : "prawdopodobny"} mocz</span><small>${last ? `${dateLabel(last.exited_at)} · ${last.source === "mock" ? "symulacja" : "kamera"}` : "Brak takich zdarzeń"}</small></div></div><p class="cat-last-assessment">${visits[0] ? `Ostatnia wizyta · ${dateLabel(visits[0].entered_at)} ${time(visits[0].entered_at)}<br><strong>${escape(state.outcomes[visits[0].outcome] || "Wizyta trwa")}</strong>` : "Brak wizyt"}</p></div></a>`;
    })
    .join("");
}
function renderCatProfile() {
  const cat = state?.cats.find(c => c.id === selectedCat);
  if (!cat) return;
  const visits = visibleVisits().filter(v => v.cat_id === cat.id);
  $("#cat-profile-title").textContent = cat.name;
  $("#cat-profile-content").innerHTML = `<p>${escape(cat.description)}</p><p>${visits.length} wizyt · godziny w strefie Europe/Warsaw</p><h3>Historia wizyt</h3><ol class="profile-visits">${visits.map(v => {
    const segments = [...new Map((v.segments || [v]).map(part => [part.id.split("-")[0], part])).values()];
    const films = v.source === "camera" ? segments.map((part, i) => `<a class="button secondary" href="/api/recordings/${encodeURIComponent(part.id.split("-")[0])}/video" target="_blank" rel="noopener">▶ Odtwórz film${segments.length > 1 ? ` ${i + 1}` : ""}</a>`).join("") : '<span class="muted">Symulacja · brak filmu</span>';
    return `<li><div class="profile-visit-heading"><strong>${escape(day(v.entered_at))} · ${time(v.entered_at)}–${v.exited_at ? time(v.exited_at) : "trwa"}</strong><span>${duration(v)}</span></div><p>${trayLabel(v)} · ${v.source === "mock" ? "symulacja" : "kamera"}</p><p class="result ${v.outcome}">${escape(state.outcomes[v.outcome] || "Wizyta trwa")}</p>${v.reviewed_at ? `<p class="muted">Twoja ocena · ${escape(day(v.reviewed_at))} ${time(v.reviewed_at)}</p>` : ""}<div class="profile-visit-actions">${films}<button class="text-button" data-visit="${escape(v.id)}">Szczegóły i ocena</button></div></li>`;
  }).join("") || '<li class="empty">Ten kot nie ma jeszcze zapisanych wizyt.</li>'}</ol>`;
}
function openCatProfile(id) {
  if (!state?.cats.some(c => c.id === id)) return;
  selectedCat = id;
  renderCatProfile();
  if (!$("#cat-profile-dialog").open) $("#cat-profile-dialog").showModal();
}
function mapSvg(box, visits, active = null, detail = false) {
  const id = `grain-${box.id}-${detail ? "detail" : "main"}`;
  const marks = wasteMarks(visits);
  const proposals = proposedMarks(visits);
  const counts = {};
  const patches = marks
    .map((v) => {
      const overlap = counts[v.region] || 0;
      counts[v.region] = overlap + 1;
      const x = v.point ? 32 + 236 * v.point[0] : 68 + (v.region % 3) * 80 + ((overlap % 3) - 1) * 9;
      const y = v.point ? 29 + 161 * v.point[1] :
        62 + Math.floor(v.region / 3) * 49 + (Math.floor(overlap / 3) % 3) * 5;
      return `<a href="#" data-visit="${v.id}" aria-label="${v.kind}, ${escape(v.cat_id)}: ${escape(state.regions[v.region])}, ${time(v.entered_at)}">${v.kind === "Kał" ? `<rect class="patch" x="${x - 23}" y="${y - 12}" width="46" height="24" rx="7" fill="${v.cat_id === "kefir" ? "#839c70" : "#cb9775"}" stroke="#694c37" stroke-width="3"/>` : `<ellipse class="patch" cx="${x}" cy="${y}" rx="${v.cat_id === "kefir" ? 23 : 18}" ry="${v.cat_id === "kefir" ? 15 : 13}" transform="rotate(-20 ${x} ${y})" fill="${v.cat_id === "kefir" ? "#839c70" : "#cb9775"}"/>`}<text x="${x}" y="${y + 3}" text-anchor="middle" fill="white" font-size="9" font-weight="600">${catShort(v.cat_id)} · ${v.kind === "Kał" ? "K" : "M"}</text></a>`;
    })
    .join("");
  const proposalCounts = {};
  const proposedPatches = proposals.map(v => {
    const overlap = proposalCounts[v.region] || 0;
    proposalCounts[v.region] = overlap + 1;
    const x = 68 + (v.region % 3) * 80 + (overlap % 2 ? 20 : -20);
    const y = 62 + Math.floor(v.region / 3) * 49 + Math.floor(overlap / 2) % 2 * 20;
    return `<a href="#" data-visit="${v.id}" aria-label="Proponowane miejsce, ${catName(v.cat_id)}: ${escape(state.regions[v.region])}, ${time(v.entered_at)}; mocz lub kał niepotwierdzone"><rect class="proposal-patch" x="${x - 18}" y="${y - 10}" width="36" height="20" rx="6"/><text x="${x}" y="${y + 3}" text-anchor="middle" fill="#775419" font-size="9" font-weight="600">${catShort(v.cat_id)} · P</text></a>`;
  }).join("");
  return `<svg class="litter-map" viewBox="0 0 300 228" role="img" aria-label="Schemat kuwety ${box.id}; ${marks.length} oznaczeń moczu i kału; ${proposals.length} propozycji do oceny"><defs><pattern id="${id}" width="9" height="9" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r=".7" fill="#d2d3ba"/><circle cx="7" cy="6" r=".5" fill="#dddec8"/></pattern></defs><rect x="21" y="18" width="258" height="183" rx="31" fill="#f1f0e5" stroke="#e1e2d5" stroke-width="2"/><rect x="32" y="29" width="236" height="161" rx="24" fill="#e9e9d8"/><rect x="32" y="29" width="236" height="161" rx="24" fill="url(#${id})"/><path d="M108 34v150m80-150v150M37 87h226M37 136h226" stroke="#cbd1b9" stroke-dasharray="3 4" opacity=".55"/>${patches}${proposedPatches}${!marks.length && !proposals.length && !active ? '<text class="map-empty" x="150" y="108" text-anchor="middle">Brak oznaczeń moczu i kału</text>' : ""}${active ? `<g class="occupied-cat" transform="translate(118 69)"><ellipse cx="32" cy="33" rx="22" ry="34" fill="${active.cat_id === "kefir" ? "#62735a" : "#b58b69"}"/><path d="M14 13 12-3l16 9 13-9 7 20" fill="${active.cat_id === "kefir" ? "#62735a" : "#b58b69"}"/><path d="M30 62c35 19 42-9 26-10" fill="none" stroke="${active.cat_id === "kefir" ? "#62735a" : "#b58b69"}" stroke-width="8" stroke-linecap="round"/></g>` : ""}<rect x="118" y="191" width="64" height="13" rx="6" fill="#fdfdf9" stroke="#e1e2d5"/><text class="map-label" x="150" y="221" text-anchor="middle">DÓŁ OBRAZU</text></svg>`;
}
function renderBoxes() {
  $("#boxes").innerHTML = state.boxes
    .map((box) => {
      const visits = visibleVisits().filter((v) => v.box_id === box.id);
      const active = visits.find((v) => v.status === "active");
      const marks = visits.filter(
        (v) =>
          v.exited_at &&
          (!box.cleaned_at ||
            Date.parse(v.exited_at) > Date.parse(box.cleaned_at)),
      );
      const count = wasteMarks(marks).length;
      const proposalCount = proposedMarks(marks).length;
      return `<article class="box-card"><div class="box-header"><div><h3>${escape(box.name)}</h3><p>Schemat · górny rząd odpowiada górze obrazu</p></div><span class="box-status ${active ? "occupied" : ""}">${active ? escape(active.cat_id === "kefir" ? "Kefir" : "Kalinka") : "Brak oceny na żywo"}</span></div>${mapSvg(box, marks, active)}<div class="box-bottom"><span>${active ? `Wizyta trwa · <span data-duration="${active.id}">${duration(active)}</span>` : `${count} oznaczeń od sprzątania${proposalCount ? ` · propozycje do oceny: ${proposalCount}` : ""}`}</span><button class="text-button" data-clean="${box.id}" ${active ? "disabled" : ""}>Oznacz sprzątanie</button></div></article>`;
    })
    .join("");
}
function renderVisits() {
  const cat = $("#cat-filter").value,
    result = $("#result-filter").value;
  const visits = visibleVisits().filter(
    (v) =>
      (cat === "all" || v.cat_id === cat) &&
      (result === "all" || v.outcome === result),
  );
  $("#visits").innerHTML = visits.length
    ? visits
        .map(
          (v) =>
            `<tr><td><div class="cat-table"><span class="mini-avatar ${v.cat_id}">${catShort(v.cat_id)}</span><div><span class="table-cat-name">${catName(v.cat_id)}</span><span class="table-sub">${trayLabel(v)} · ${v.source === "mock" ? "symulacja" : "kamera · czas przybliżony"}</span></div></div></td><td><span class="table-time">${time(v.entered_at)} <span class="muted">→</span> ${v.exited_at ? time(v.exited_at) : "trwa"}</span><span class="table-sub">${dateLabel(v.entered_at)}</span></td><td><span ${v.status === "active" ? `data-duration="${v.id}"` : ""}>${duration(v)}</span></td><td><span class="result ${v.status === "active" ? "active" : v.outcome}">${v.status === "active" ? "Wizyta trwa" : escape(state.outcomes[v.outcome])}</span>${proposalLabel(v)}</td><td><button class="row-open" data-visit="${v.id}" aria-label="Szczegóły wizyty ${escape(v.cat_id)} o ${time(v.entered_at)}">↗</button></td></tr>`,
        )
        .join("")
    : '<tr><td colspan="5" class="empty">Brak wizyt dla wybranych filtrów.</td></tr>';
  $("#visit-count").textContent =
    `${visits.length} wizyt · symulacje oznaczone osobno · godziny w strefie Europe/Warsaw`;
}
function renderNotices() {
  $("#notices").innerHTML =
    state.notices
      .slice(0, 6)
      .map(
        (n) =>
          `<li class="notice"><time datetime="${n.created_at}">${time(n.created_at)} · ${dateLabel(n.created_at)}</time><p>${n.visit_id ? `<button data-visit="${n.visit_id}">${escape(n.text)}</button>` : escape(n.text)}</p></li>`,
      )
      .join("") || '<li class="muted">Tutaj pojawią się nowe zdarzenia.</li>';
}
function acceptState(next) {
  state = next;
  serverOffset = Date.parse(next.server_time) - Date.now();
  if (lastNotice !== null) {
    const fresh = next.notices.filter((n) => n.id > lastNotice).reverse();
    for (const n of fresh) {
      toast(n.text);
      if (
        "Notification" in window &&
        Notification.permission === "granted" &&
        document.hidden
      ) {
        const notice = new Notification("Kuweta", {
          body: n.text,
          tag: `kuweta-${n.id}`,
          icon: "/favicon.svg",
        });
        notice.onclick = () => {
          window.focus();
          if (n.visit_id) openVisit(n.visit_id);
          notice.close();
        };
      }
    }
  }
  lastNotice = Math.max(lastNotice || 0, ...next.notices.map((n) => n.id));
  renderCats();
  if (selectedCat) renderCatProfile();
  renderBoxes();
  renderVisits();
  renderNotices();
  renderRecordings();
  $("#qwen-status").textContent = next.qwen.configured
    ? (next.recordings?.items.some(r => r.status === "waiting_model")
      ? "Qwen: nagrania oczekują na model · automatyczne ponawianie co minutę"
      : `Qwen: ${next.qwen.model} · automatyczna analiza nagrań · wyniki do weryfikacji`)
    : "Qwen: adapter gotowy · połączenie nieskonfigurowane";
  $("#today").textContent = new Date()
    .toLocaleDateString("pl-PL", {
      timeZone: "Europe/Warsaw",
      weekday: "long",
      day: "numeric",
      month: "long",
      year: "numeric",
    })
    .toUpperCase();
  if (selectedVisit && $("#visit-dialog").open && !$("#review-form"))
    openVisit(selectedVisit, true);
}
function renderRecordings() {
  const recordings = state.recordings || {};
  const checked = recordings.last_checked_at;
  $("#recordings-status").textContent = recordings.error
    ? `${recordings.error}${checked ? ` Ostatni poprawny odczyt: ${dateLabel(checked)} ${time(checked, true)}.` : ""}`
    : checked ? `Sprawdzanie co około minutę · ostatni odczyt ${dateLabel(checked)} ${time(checked, true)}`
    : "Oczekiwanie na pierwsze sprawdzenie nagrań…";
  if (recordings.clock_offset_seconds == null && checked) {
    $("#recordings-status").textContent += " · Nie udało się zweryfikować zegara kamery.";
  } else if (Math.abs(recordings.clock_offset_seconds || 0) > 5) {
    $("#recordings-status").textContent += " · Zegar kamery różni się od komputera — sprawdź synchronizację czasu.";
  }
  const labels = {missing_on_camera: "Nagranie niedostępne na karcie · pominięte", waiting_model: "Oczekuje na dostęp do Qwena · wznowienie automatyczne", downloading: "Pobieranie filmu", analyzing: "Qwen analizuje film", analyzed: "Analiza zakończona · sprawdź historię wizyt", no_cat_observed: "Nie wykryto kota w próbkowanych klatkach", needs_review: "Materiał wymaga ręcznej oceny", retry: "Błąd · oczekuje ponowienia", failed: "Analiza zatrzymana po 3 nieudanych próbach", ignored_test: "Test ruchu z człowiekiem · pominięty", recording: "Nagranie jeszcze się kończy", pending_analysis: "Oczekuje pobrania i analizy · kot niepotwierdzony"};
  $("#camera-recordings").innerHTML = (recordings.items || []).map(r =>
    `<li class="notice"><time datetime="${escape(r.started_at)}">${dateLabel(r.started_at)} · ${time(r.started_at, true)} → ${time(r.ended_at, true)} · ${r.end_epoch - r.start_epoch} s</time><p>${escape(labels[r.status] || "Oczekuje oceny")}${r.error ? ` · ${escape(r.error)}` : ""}</p>${r.analysis ? `<p>${escape(JSON.parse(r.analysis).summary)}</p>` : ""}${r.media_key ? `<a href="/api/recordings/${escape(r.media_key)}/video" target="_blank" rel="noopener">Otwórz film źródłowy</a>` : ""}</li>`
  ).join("") || '<li class="muted">Nie znaleziono nagrań. Nowe pojawią się tutaj automatycznie.</li>';
}
function anatomyEvidence(focus, key, accepted = false) {
  const checks = (focus?.pose_checks || []).filter(c => c.file);
  if (!checks.length) return "";
  const validPoint = point => Array.isArray(point) && point.length === 2 && point.every(n => Number.isFinite(n) && n >= 0 && n <= 1);
  const mark = point => validPoint(point)
    ? `<g class="anatomy-tail" aria-label="Nasada ogona według Qwena"><circle cx="${point[0] * 100}%" cy="${point[1] * 100}%" r="6" data-point-x="${point[0]}" data-point-y="${point[1]}" data-point-label="Z"/><text x="${point[0] * 100}%" y="${point[1] * 100}%" dy="${point[1] < .06 ? 25 : -14}" text-anchor="middle">Z</text></g>` : "";
  return `<section class="posture-review"><h3>${accepted ? "Miejsce sugerowane przez Qwena · Z" : "Qwen · nasada ogona"}</h3>
    <p>Z = nasada ogona wskazana przez Qwena, bez Twoich oznaczeń M/K. Dokładne X/Y liczymy od lewego górnego rogu zdjęcia, bez siatki 3 × 3. Widoczność głowy nie jest wymagana.</p>
    ${accepted ? "<p>Położenie Z było spójne w trzech klatkach. Wokół tego punktu aplikacja szuka śladów moczu lub kału.</p>" : "<p>Model osobno ocenia widoczność nasady ogona. Może wskazać trafny punkt, mimo że opisuje go jako niewyraźny. Ta samoocena nie rozstrzyga, czy punkt jest poprawny. Wskazanie Z nie potwierdza oddania moczu.</p>"}
    <div class="posture-photos">${checks.map(c => `<figure>
      <div class="posture-photo"><img loading="lazy" src="/api/recordings/${encodeURIComponent(key)}/evidence/${encodeURIComponent(c.file)}" alt="Niezależna analiza Qwena w ${escape(c.t)} sekundzie"><svg class="photo-points" aria-label="Punkty analizy na zdjęciu">${mark(c.tail_base_point)}</svg></div>
      <figcaption><a href="/api/recordings/${encodeURIComponent(key)}/video#t=${encodeURIComponent(c.t)}" target="_blank" rel="noopener">Klatka ${escape(c.t)} s / otwórz film</a> · ${c.reason === "outside_tray" ? "Odrzucono wskazanie Qwena poza zaznaczonym obszarem kuwety. Brak wiarygodnego punktu Z." : validPoint(c.tail_base_point) ? "Qwen wskazał nasadę ogona (Z)." : "Qwen nie podał punktu nasady ogona."}<span class="photo-coordinates"></span>
        <details><summary>Samoocena Qwena i wcześniejsza analiza P</summary>
          <p>Widoczność według modelu: ${c.reason === "restored_point" ? "brak zachowanej samooceny wcześniejszego wyniku" : c.clear ? "wyraźna" : "niejednoznaczna"}. Komentarz modelu: ${escape(c.explanation || "brak")}</p>
          <p>P to wcześniejszy kandydat z ogólnej analizy filmu, a nie nowe wskazanie nasady ogona. ${validPoint(c.candidate_point) ? `<span class="prior-coordinates" data-point-x="${c.candidate_point[0]}" data-point-y="${c.candidate_point[1]}"></span>` : "Brak współrzędnych P."}</p>
          <p>${c.reason === "restored_point" ? "Przywrócono wcześniejszy punkt Z. Wynik spójności z ostatniego eksperymentu został usunięty." : c.verification === "temporal_tail_base" ? (c.accepted ? "Z ma spójne położenie w trzech klatkach i wyznacza obszar dalszej analizy. Z nie jest porównywane z P." : "Nie potwierdzono spójnego położenia Z w trzech klatkach. Z nie jest porównywane z P.") : (c.accepted ? "Wcześniejszy kandydat P przeszedł weryfikację." : "Wcześniejszy kandydat P nie przeszedł weryfikacji. Nie przesądza to o poprawności nowego punktu Z.")}</p>
        </details>
      </figcaption></figure>`).join("")}</div></section>`;
}
function focusEvidence(v) {
  if (v.source !== "camera") return "";
  if (v.segments?.length > 1) {
    return `<section><h3>Jedna wizyta · ${v.segments.length} fragmenty obserwacji</h3><p>Przejścia między kuwetami w tym samym filmie należą do jednej wizyty. Poniżej są jej fragmenty; historia i podsumowanie liczą wizytę jeden raz.</p>${v.segments.map((segment, i) => `<h4>Fragment ${i + 1} · kuweta ${segment.box_id || v.box_id} · ${time(segment.entered_at, true)}–${time(segment.exited_at, true)}</h4><p><a href="/api/recordings/${encodeURIComponent(segment.id.split("-")[0])}/video" target="_blank" rel="noopener">Otwórz film ${i + 1}</a></p>${focusEvidence({...v, ...segment, segments: null})}`).join("")}</section>`;
  }
  const [key, index] = v.id.split("-");
  const recording = state.recordings?.items.find(r => r.media_key === key);
  if (!recording?.analysis) return "";
  const analysis = JSON.parse(recording.analysis);
  const focus = analysis.visits?.[Number(index)]?.focus;
  const anatomy = anatomyEvidence(focus, key);
  const newerAnalysis = v.reviewed_at && analysis.completed_at && new Date(analysis.completed_at) > new Date(v.reviewed_at);
  const analysisNotice = newerAnalysis ? `<p class="muted">Ponowna analiza · ${time(analysis.completed_at)}. Twoje oznaczenia M/K pozostają zapisane osobno.</p>` : "";
  if (!focus?.windows?.length) return `${anatomy}${newerAnalysis ? `${analysisNotice}<p>Samo wskazanie nasady ogona Z nie potwierdza miejsca moczu. Automatyczna analiza śladów nie ustaliła go dla tej wizyty.</p>` : ""}`;
  const acceptedChecks = (focus.pose_checks || []).filter(c => c.accepted && c.verification === "temporal_tail_base");
  const suggestion = acceptedChecks.length
    ? anatomyEvidence({...focus, pose_checks: acceptedChecks}, key, true)
    : "<p>Brak zweryfikowanej sugestii Z. Wcześniejszy obszar analizy nie jest wskazaniem nasady ogona.</p>";
  const previousAnalysis = v.reviewed_at && !newerAnalysis;
  return `${analysisNotice}${suggestion}<details><summary>Wszystkie wskazania Qwena · szczegóły weryfikacji</summary>${anatomy}</details>${previousAnalysis ? "<details><summary>Pierwotna analiza modelu · przed ręczną oceną</summary>" : ""}<section class="focus-evidence"><h3>Analiza śladów wokół wskazanego miejsca</h3><p>Samo wskazanie nasady ogona Z nie potwierdza oddania moczu ani kału.</p>${focus.evidence.length ? focus.evidence.map(e => {
    const url = `/api/recordings/${encodeURIComponent(key)}/evidence/`;
    return `<h4>${e.kind === "urine" ? "Prawdopodobny mocz" : "Prawdopodobny kał"} · ${escape(e.t)} s filmu</h4><div class="evidence-pair"><figure><img loading="lazy" src="${url}${encodeURIComponent(e.before_file)}" alt="Stały obszar przed wizytą"><figcaption>Przed wizytą · ${escape(e.before_t)} s</figcaption></figure><figure><img loading="lazy" src="${url}${encodeURIComponent(e.file)}" alt="Ten sam obszar z możliwym śladem"><figcaption>Zaobserwowany ślad · ${escape(e.t)} s</figcaption></figure></div><p>${escape(e.note)} <a href="/api/recordings/${encodeURIComponent(key)}/video#t=${encodeURIComponent(e.t)}" target="_blank" rel="noopener">Zobacz moment w filmie</a></p>`;
  }).join("") : "<p>Nie znaleziono jednoznacznego śladu w wybranych klatkach tego obszaru.</p>"}${focus.limited ? "<p>Analiza śladów obejmuje maksymalnie 3 fragmenty. Pozostałe wymagają ręcznej oceny.</p>" : ""}</section>${previousAnalysis ? "</details>" : ""}`;
}

async function loadTailEditor(visitId, cat) {
  const section = $("#tail-editor");
  if (!section) return;
  try {
    const response = await fetch(`/api/visits/${encodeURIComponent(visitId)}/tail-labels`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Nie udało się pobrać klatek.");
    if (!section.isConnected) return;
    section.innerHTML = `<h3>Popraw położenie Z · ${escape(cat)}</h3>
      <p>Kliknij lub dotknij środka miejsca, gdzie ogon łączy się z tułowiem, a następnie zapisz punkt pod zdjęciem. Strzałki przesuwają punkt o jeden piksel.</p>
      <p><strong>Z</strong> — aktualne wskazanie Qwena. <strong>R</strong> — Twój punkt referencyjny. Zapis dotyczy tylko tej klatki; nie zmienia oznaczeń moczu ani kału i nie uczy automatycznie modelu.</p>
      <div class="posture-photos tail-photos"></div>`;
    const list = section.querySelector(".tail-photos");
    if (!data.frames.length) { list.textContent = "Brak klatek nasady ogona dla tej wizyty."; return; }
    for (const frame of data.frames) {
      const figure = document.createElement("figure");
      let point = frame.point;
      let savedPoint = frame.point;
      const base = `/api/recordings/${encodeURIComponent(data.media_key)}`;
      figure.innerHTML = `<div class="posture-photo"><img src="${base}/evidence/${encodeURIComponent(frame.file)}?v=${encodeURIComponent(frame.image_sha)}" alt="${escape(cat)} · nasada ogona · klatka ${escape(frame.t)} s">
        <button type="button" class="tail-canvas" aria-label="Popraw Z: ${escape(cat)}, klatka ${escape(frame.t)} s"></button><svg class="photo-points" aria-label="Porównanie Qwena z Twoim punktem"></svg></div>
        <figcaption><a href="${base}/video#t=${encodeURIComponent(frame.t)}" target="_blank" rel="noopener">Klatka ${escape(frame.t)} s / otwórz film</a><p class="tail-coordinates"></p>
        <div class="tail-actions"><button type="button" class="button primary tail-save">Zapisz punkt R</button><button type="button" class="button tail-clear">Usuń punkt R</button></div><p class="tail-feedback" role="status"></p></figcaption>`;
      list.append(figure);
      const img = figure.querySelector("img"), canvas = figure.querySelector(".tail-canvas");
      const save = figure.querySelector(".tail-save"), clear = figure.querySelector(".tail-clear");
      const feedback = figure.querySelector(".tail-feedback");
      function render() {
        const valid = p => Array.isArray(p) && p.length === 2 && p.every(n => Number.isFinite(n) && n >= 0 && n <= 1);
        const marks = [["Z", frame.model_point, "anatomy-tail"], ["R", point, "tail-reference"]].filter(([, p]) => valid(p));
        figure.querySelector("svg").innerHTML = marks.map(([label, p, cls]) => `<g class="${cls}"><circle cx="${p[0] * 100}%" cy="${p[1] * 100}%" r="6"/><text x="${p[0] * 100}%" y="${p[1] * 100}%" dy="${label === "R" ? 24 : -14}" text-anchor="middle">${label}</text></g>`).join("");
        const w = img.naturalWidth, h = img.naturalHeight;
        const pixels = p => [Math.min(w - 1, Math.round(p[0] * w)), Math.min(h - 1, Math.round(p[1] * h))];
        let text = w ? marks.map(([label, p]) => { const [x,y] = pixels(p); return `${label}: X = ${x} px, Y = ${y} px`; }).join(" · ") : "Ładowanie zdjęcia…";
        if (w && valid(point) && valid(frame.model_point)) {
          const [x,y] = pixels(point), [mx,my] = pixels(frame.model_point);
          text += ` · Odległość Z–R: ${Math.hypot(x - mx, y - my).toFixed(1)} px`;
        }
        figure.querySelector(".tail-coordinates").textContent = text;
        save.disabled = !point || JSON.stringify(point) === JSON.stringify(savedPoint);
        clear.disabled = !point && !savedPoint;
      }
      function choose(p) {
        point = p.map(n => Math.round(Math.max(0, Math.min(1, n)) * 1000000) / 1000000);
        feedback.textContent = "Punkt R zmieniony — kliknij „Zapisz punkt R”.";
        render();
      }
      canvas.addEventListener("click", event => {
        if (!img.naturalWidth) return;
        const rect = canvas.getBoundingClientRect();
        choose(event.detail === 0 ? point || frame.model_point || [.5,.5] : [(event.clientX - rect.left) / rect.width, (event.clientY - rect.top) / rect.height]);
      });
      canvas.addEventListener("keydown", event => {
        if (!img.naturalWidth) return;
        const delta = {ArrowLeft: [-1,0], ArrowRight: [1,0], ArrowUp: [0,-1], ArrowDown: [0,1]}[event.key];
        if (!delta) return;
        event.preventDefault();
        const p = point || frame.model_point || [.5,.5];
        choose([p[0] + delta[0] / img.naturalWidth, p[1] + delta[1] / img.naturalHeight]);
      });
      async function persist(value) {
        save.disabled = clear.disabled = canvas.disabled = true;
        try {
          await post(`/api/visits/${encodeURIComponent(visitId)}/tail-labels`, {file: frame.file, image_sha: frame.image_sha, point: value});
          point = savedPoint = value;
          feedback.textContent = value ? "Zapisano punkt referencyjny R. Wskazanie Qwena pozostaje bez zmian." : "Usunięto punkt referencyjny R.";
        } catch (error) { feedback.textContent = error.message; }
        finally { canvas.disabled = false; render(); }
      }
      save.addEventListener("click", () => persist(point));
      clear.addEventListener("click", () => persist(null));
      img.addEventListener("load", render);
      img.addEventListener("error", () => { canvas.disabled = true; feedback.textContent = "Nie udało się wczytać zdjęcia. Otwórz wizytę ponownie."; });
      feedback.textContent = savedPoint ? "Wczytano zapisany punkt referencyjny R." : "Brak Twojego punktu — wskaż nasadę ogona.";
      render();
    }
  } catch (error) { if (section.isConnected) section.textContent = error.message; }
}

function reviewLocationEditor(v) {
  const [key, index] = v.id.split("-");
  const recording = state.recordings?.items.find(r => r.media_key === key);
  const focus = recording?.analysis ? JSON.parse(recording.analysis).visits?.[Number(index)]?.focus : null;
  const photos = focus?.suggestions?.length ? focus.suggestions : (focus?.pose_checks || [])
    .filter(c => c.file).map(c => ({file: c.file, t: c.t, point: c.tail_base_point, location_source: "tail_base"}));
  return `<section class="posture-review"><h4>Miejsce na zdjęciu</h4><p>Z to wskazanie nasady ogona przez Qwena. M i K to Twoje oznaczenia moczu i kału. Szeroka ramka oznacza zapisany wcześniej, przybliżony obszar.</p>${photos.length ? `<label>Zaznacz miejsce dla<select id="location-kind"><option value="region">Moczu</option><option value="feces_region">Kału</option></select></label><p>Kliknij lub dotknij dokładnego miejsca na zdjęciu. Kolejne kliknięcie przesuwa punkt. Klawiaturą: Enter wybiera punkt, strzałki go przesuwają. Współrzędne X/Y liczymy od lewego górnego rogu zdjęcia.</p><div class="posture-photos">${photos.map(s => posturePhoto(s, key, true)).join("")}</div>` : "<p>Brak klatki do wskazania dokładnego miejsca. Dotychczasowe oznaczenia pozostają zapisane.</p>"}<p id="location-feedback" role="status">Zmiany zostaną zapisane po kliknięciu „Zapisz ocenę”. Samo wskazanie miejsca nie potwierdza wydalenia.</p></section>${["region", "feces_region"].map(field => `<div class="review-location"><strong>${field === "region" ? "Miejsce moczu (M)" : "Miejsce kału (K)"}</strong><input type="hidden" name="${field}" value="${v[field] ?? ""}"><output data-location-output="${field}"></output><button type="button" class="button" data-clear-location="${field}">Usuń oznaczenie ${field === "region" ? "moczu" : "kału"}</button></div>`).join("")}`;
}

const pointField = field => field === "region" ? "urine_point" : "feces_point";
const pointRegion = point => Math.min(2, Math.floor(point[1] * 3)) * 3 + Math.min(2, Math.floor(point[0] * 3));
const regionCenter = region => [(region % 3 + .5) / 3, (Math.floor(region / 3) + .5) / 3];
function formPoint(form, field) {
  const value = form.elements[pointField(field)].value;
  return value ? JSON.parse(value) : null;
}
function posturePhoto(s, key, showProposal) {
  const point = s.point || (Number.isInteger(s.region) ? regionCenter(s.region) : null);
  return `<figure><div class="posture-photo"><img loading="lazy" src="/api/recordings/${encodeURIComponent(key)}/evidence/${encodeURIComponent(s.file)}" alt="Kot w kuwecie w ${escape(s.t)} sekundzie nagrania"><button type="button" class="posture-canvas" aria-label="Wskaż miejsce na zdjęciu z ${escape(s.t)} sekundy"></button>${showProposal && point ? `<span class="photo-mark point suggested" data-point-x="${point[0]}" data-point-y="${point[1]}" aria-hidden="true"><b>${s.location_source === "tail_base" ? "Z" : "P"}</b></span>` : ""}<span class="photo-mark" data-photo-mark="region" hidden aria-hidden="true"><b>M</b></span><span class="photo-mark feces" data-photo-mark="feces_region" hidden aria-hidden="true"><b>K</b></span></div><figcaption>${Number.isFinite(s.stationary_seconds) ? `Postój ok. ${escape(s.stationary_seconds)} s · ` : ""}<a href="/api/recordings/${encodeURIComponent(key)}/video#t=${encodeURIComponent(s.t)}" target="_blank" rel="noopener">Klatka ${escape(s.t)} s / otwórz film</a></figcaption></figure>`;
}
function updatePhotoCoordinates(img) {
  const figure = img.closest("figure"), caption = figure?.querySelector(".photo-coordinates");
  if (!caption || !img.naturalWidth || !img.naturalHeight) return;
  const width = img.naturalWidth, height = img.naturalHeight;
  const points = Array.from(figure.querySelectorAll("circle[data-point-label]")).map(mark => {
    const x = Math.min(width - 1, Math.round(Number(mark.dataset.pointX) * width));
    const y = Math.min(height - 1, Math.round(Number(mark.dataset.pointY) * height));
    return `${mark.dataset.pointLabel}: X = ${x} px, Y = ${y} px`;
  });
  caption.textContent = `Zdjęcie ${width} × ${height} px. ${points.join(" · ")}`;
  const prior = figure.querySelector(".prior-coordinates");
  if (prior) {
    const x = Math.min(width - 1, Math.round(Number(prior.dataset.pointX) * width));
    const y = Math.min(height - 1, Math.round(Number(prior.dataset.pointY) * height));
    prior.textContent = `P: X = ${x} px, Y = ${y} px.`;
  }
}
// Lazy images can finish loading after the dialog has been rendered.
document.addEventListener("load", event => {
  if (event.target.matches?.(".posture-photo img")) updateLocationMarkers();
}, true);
function updateLocationMarkers() {
  document.querySelectorAll(".posture-photo img").forEach(updatePhotoCoordinates);
  document.querySelectorAll('.photo-mark[data-point-x][data-point-y]').forEach(mark => {
    mark.style.left = `${Number(mark.dataset.pointX) * 100}%`;
    mark.style.top = `${Number(mark.dataset.pointY) * 100}%`;
  });
  const form = $("#review-form");
  if (!form) return;
  const outcome = form.elements.outcome.value;
  for (const [field, outcomes] of [["region", urineOutcomes], ["feces_region", fecesOutcomes]]) {
    const region = form.elements[field].value, point = formPoint(form, field);
    const output = form.querySelector(`[data-location-output="${field}"]`);
    if (output) {
      const img = form.querySelector(".posture-photo img");
      output.textContent = !outcomes.includes(outcome) || region === "" ? "Nieustalone" : point
        ? (img?.naturalWidth ? `X = ${Math.min(img.naturalWidth - 1, Math.round(point[0] * img.naturalWidth))} px, Y = ${Math.min(img.naturalHeight - 1, Math.round(point[1] * img.naturalHeight))} px · pierwsze zdjęcie ${img.naturalWidth} × ${img.naturalHeight} px` : `Punkt: ${Math.round(point[0] * 100)}% od lewej, ${Math.round(point[1] * 100)}% od góry`)
        : `Wcześniejszy obszar przybliżony: ${state.regions[Number(region)]}. Wskaż dokładny punkt na zdjęciu.`;
      form.querySelector(`[data-clear-location="${field}"]`).disabled = region === "";
    }
    document.querySelectorAll(`[data-photo-mark="${field}"]`).forEach(mark => {
      mark.hidden = !outcomes.includes(outcome) || region === "";
      if (mark.hidden) return;
      const position = point || regionCenter(Number(region));
      mark.classList.toggle("point", !!point);
      mark.classList.toggle("area", !point);
      mark.style.left = `${position[0] * 100}%`;
      mark.style.top = `${position[1] * 100}%`;
    });
  }
}
function chooseLocation(field, region, point = null) {
  const form = $("#review-form");
  form.elements[field].value = String(region);
  form.elements[pointField(field)].value = point ? JSON.stringify(point) : "";
  const outcome = form.elements.outcome.value;
  const urine = field === "region" || urineOutcomes.includes(outcome);
  const feces = field === "feces_region" || fecesOutcomes.includes(outcome);
  // Adding a type of waste proposes it; only an explicit outcome choice confirms it.
  if (urine && feces && !["both", "both_confirmed"].includes(outcome)) form.elements.outcome.value = "both";
  else if (urine && !feces && !["urine", "confirmed"].includes(outcome)) form.elements.outcome.value = "urine";
  else if (feces && !urine && !["feces", "feces_confirmed"].includes(outcome)) form.elements.outcome.value = "feces";
  updateLocationMarkers();
  $("#location-feedback").textContent = `${field === "region" ? "Mocz" : "Kał"}: ${point ? `punkt na zdjęciu (${Math.round(point[0]*100)}% od lewej, ${Math.round(point[1]*100)}% od góry)` : `przybliżony obszar: ${state.regions[region]}`}. Wynik: ${state.outcomes[form.elements.outcome.value]}. Kliknij „Zapisz ocenę”.`;
}
function choosePhotoPoint(point) {
  point = point.map(n => Math.round(Math.min(1, Math.max(0, n)) * 10000) / 10000);
  chooseLocation($("#location-kind").value, pointRegion(point), point);
}

function openVisit(id, refresh = false) {
  const v = state?.visits.find((v) => v.id === id);
  if (!v) return;
  selectedVisit = id;
  $("#detail-title").textContent =
    `${catName(v.cat_id)} · ${trayLabel(v)}`;
  const active = v.status === "active";
  const explanations = {
    urine: "Symulator wygenerował nową mokrą plamę.",
    feces: "Symulator wygenerował nowe bryły kału. Nie zarejestrowano oznak moczu.",
    both: "Symulator wygenerował mokrą plamę i kał, z osobnymi miejscami na mapie.",
    empty: "Scenariusz wejścia i wyjścia bez zarejestrowanych oznak moczu ani kału.",
    uncertain: "Wynik niewidoczny lub symulacja przerwana. Nie można ocenić moczu ani kału.",
  };
  const explanation = v.source === "camera" ? escape(v.note) : active ? "Trwa symulacja. Wynik pojawi się po wyjściu kota." :
    `${explanations[v.original_outcome] || explanations.uncertain} Nie analizowano obrazu ani filmu.`;
  $("#visit-detail").innerHTML =
    `<span class="result ${active ? "active" : v.outcome}">${active ? "Wizyta trwa" : escape(state.outcomes[v.outcome])}</span><div class="detail-stats"><div><span>${v.source === "camera" ? "Pierwsza obserwacja" : "Wejście"} · ${dateLabel(v.entered_at)}</span><strong>${time(v.entered_at, true)}</strong></div><div><span>${v.source === "camera" ? "Ostatnia obserwacja" : "Wyjście"}</span><strong>${time(v.exited_at, true)}</strong></div><div><span>${v.source === "camera" ? "Obserwowany okres" : "Czas w kuwecie"}</span><strong ${active ? `data-duration="${v.id}"` : ""}>${duration(v)}</strong></div></div>${v.source === "camera" ? '<section id="tail-editor" class="posture-review"><h3>Popraw położenie Z</h3><p>Wczytywanie klatek…</p></section>' : ""}${focusEvidence(v)}${v.source === "camera" ? `<section class="saved-visit-assessment"><h3>${v.reviewed_at ? "Zapisana ocena użytkownika · M/K" : "Zapisany wynik wizyty"}</h3><p>${v.reviewed_at ? "Poniższy schemat przedstawia Twoje zapisane oznaczenia. Sugestia Qwena Z jest pokazana osobno na zdjęciu powyżej." : "Schemat przedstawia zapisany wynik analizy wizyty."}</p>` : ""}<div class="detail-map">${mapSvg({ id: v.box_id }, [v], active ? v : null, true)}</div><p class="detail-note">${v.source === "camera" && v.reviewed_at ? "<strong>Zapisana wcześniej ocena</strong><br>" : ""}${explanation}<br>Mocz: <strong>${locationLabel(v, "urine")}</strong>. Kał: <strong>${locationLabel(v, "feces")}</strong>. ${v.source === "camera" ? `Film źródłowy znajdziesz w sekcji nagrań. <a href="/api/recordings/${v.id.split("-")[0]}/video" target="_blank" rel="noopener">Otwórz film</a>` : "Schemat symulowany."}</p>${v.source === "camera" ? "</section>" : ""}${active
        ? ""
        : `<form id="review-form" class="review-form"><h3>Twoja ocena</h3><input type="hidden" name="urine_point" value="${escape(v.urine_point ? JSON.stringify(v.urine_point) : "")}"><input type="hidden" name="feces_point" value="${escape(v.feces_point ? JSON.stringify(v.feces_point) : "")}">${v.source === "camera" ? `<label>Kot<select name="cat_id">${["unknown", "kefir", "kalinka"].map(id => `<option value="${id}" ${id === v.cat_id ? "selected" : ""}>${catName(id)}</option>`).join("")}</select></label>` : ""}<label>Wynik<select name="outcome">${Object.entries(
            state.outcomes,
          )
            .map(
              ([key, label]) =>
                `<option value="${key}" ${key === v.outcome ? "selected" : ""}>${escape(label)}</option>`,
            )
            .join(
              "",
            )}</select></label>${v.source === "camera" ? reviewLocationEditor(v) : `<label>Miejsce moczu<select name="region"><option value="">Nieustalone</option>${state.regions.map((r, i) => `<option value="${i}" ${i === v.region ? "selected" : ""}>${escape(r)}</option>`).join("")}</select></label><label>Miejsce kału<select name="feces_region"><option value="">Nieustalone</option>${state.regions.map((r, i) => `<option value="${i}" ${i === v.feces_region ? "selected" : ""}>${escape(r)}</option>`).join("")}</select></label>`}<label>Notatka<textarea name="note" maxlength="1000" placeholder="Dodaj obserwację do tej wizyty…">${escape(v.source === "camera" && !v.reviewed_at ? "" : v.note)}</textarea></label><div class="review-actions"><span>${v.reviewed_at ? `Ostatnia ocena: ${time(v.reviewed_at)}. ` : ""}${v.source === "mock" ? "Ocena pozostanie oznaczona jako testowa." : "Ręczna ocena rzeczywistego nagrania."}</span><button class="button primary" type="submit">Zapisz ocenę</button></div><p class="form-feedback" id="review-feedback" role="status"></p></form>`
    }`;
  updateLocationMarkers();
  if (v.segments?.length > 1) {
    const selector = document.createElement("label");
    selector.textContent = "Klatki do poprawy położenia Z";
    const select = document.createElement("select");
    v.segments.forEach((segment, i) => select.add(new Option(`Fragment ${i + 1} · kuweta ${segment.box_id || v.box_id} · ${time(segment.entered_at, true)}`, segment.id)));
    select.addEventListener("change", () => loadTailEditor(select.value, catName(v.cat_id)));
    selector.append(select);
    $("#tail-editor").before(selector);
  }
  if (v.source === "camera") loadTailEditor(v.id, catName(v.cat_id));
  if (!refresh && !$("#visit-dialog").open) $("#visit-dialog").showModal();
}
function setConnected(value) {
  connected = value;
  $("#connection").className = `connection ${value ? "online" : "offline"}`;
  $("#connection").textContent = value
    ? "Połączono lokalnie"
    : "Brak połączenia";
  $("#error-banner").hidden = value;
  $("#error-banner").textContent =
    "Brak połączenia z serwerem. Widoczne dane mogą być nieaktualne. Próba ponownego połączenia…";
  $("#simulate-button").disabled = !value;
}
$("#region-picker").innerHTML = [
  "LT",
  "T",
  "PT",
  "L",
  "●",
  "P",
  "LP",
  "D",
  "PP",
]
  .map(
    (label, i) =>
      `<label class="region-option"><input type="radio" name="region" form="simulation-form" value="${i}" ${i === 2 ? "checked" : ""} aria-label="${["lewy tył", "środek z tyłu", "prawy tył", "lewy środek", "środek", "prawy środek", "lewy przód", "środek z przodu", "prawy przód"][i]}"><span>${label}</span></label>`,
  )
  .join("");
$("#scenario").addEventListener("change", () => {
  $("#feces-location").hidden = !["feces", "both"].includes($("#scenario").value);
  $("#region-picker").classList.toggle(
    "disabled",
    !["urine", "both"].includes($("#scenario").value),
  );
});
$("#simulation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!connected) return;
  const button = $("#simulate-button");
  button.disabled = true;
  const data = Object.fromEntries(new FormData(event.target));
  data.box_id = Number(data.box_id);
  data.region = Number(data.region);
  data.feces_region = Number(data.feces_region);
  try {
    await post("/api/simulate", data);
    $("#simulation-feedback").textContent =
      "Kot wszedł do kuwety. Obserwuj mapę i historię.";
  } catch (error) {
    $("#simulation-feedback").textContent = error.message;
  } finally {
    button.disabled = !connected;
  }
});
document.addEventListener("click", async (event) => {
  const profile = event.target.closest("[data-cat-profile]");
  if (profile) {
    event.preventDefault();
    openCatProfile(profile.dataset.catProfile);
    return;
  }
  const visit = event.target.closest("[data-visit]");
  if (visit) {
    event.preventDefault();
    openVisit(visit.dataset.visit);
    return;
  }
  const clean = event.target.closest("[data-clean]");
  if (clean) {
    clean.disabled = true;
    try {
      await post(`/api/boxes/${clean.dataset.clean}/clean`);
      toast("Mapa wyczyszczona. Historia wizyt została zachowana.");
    } catch (error) {
      toast(error.message);
    } finally {
      clean.disabled = false;
    }
  }
});
$("#visit-dialog").addEventListener("submit", async (event) => {
  if (event.target.id !== "review-form") return;
  event.preventDefault();
  const button = event.target.querySelector("button[type=submit]");
  button.disabled = true;
  const data = Object.fromEntries(new FormData(event.target));
  data.region = data.region === "" ? null : Number(data.region);
  data.feces_region = data.feces_region === "" ? null : Number(data.feces_region);
  for (const field of ["urine_point", "feces_point"]) data[field] = data[field] ? JSON.parse(data[field]) : null;
  try {
    await post(`/api/visits/${selectedVisit}/review`, data);
    $("#visit-dialog").close();
    toast("Zapisano ocenę wizyty.");
    // Refresh immediately even when the event stream has temporarily disconnected.
    try {
      const response = await fetch("/api/state", {cache: "no-store"});
      if (!response.ok) throw new Error("state");
      acceptState(await response.json());
    } catch {
      toast("Ocena zapisana. Nie udało się odświeżyć podsumowania — odśwież stronę.");
    }
  } catch (error) {
    $("#review-feedback").textContent = error.message;
    button.disabled = false;
  }
});
$("#visit-dialog").addEventListener("click", event => {
  const clear = event.target.closest("[data-clear-location]");
  if (clear) {
    const form = $("#review-form"), field = clear.dataset.clearLocation;
    form.elements[field].value = "";
    form.elements[pointField(field)].value = "";
    updateLocationMarkers();
    $("#location-feedback").textContent = "Usunięto oznaczenie miejsca. Kliknij „Zapisz ocenę”, aby zachować zmianę.";
    return;
  }
  const canvas = event.target.closest(".posture-canvas");
  if (!canvas) return;
  const field = $("#location-kind").value, form = $("#review-form");
  if (event.detail === 0) {
    choosePhotoPoint(formPoint(form, field) || (form.elements[field].value !== "" ? regionCenter(Number(form.elements[field].value)) : [.5, .5]));
  } else {
    const rect = canvas.getBoundingClientRect();
    choosePhotoPoint([(event.clientX - rect.left) / rect.width, (event.clientY - rect.top) / rect.height]);
  }
});
$("#visit-dialog").addEventListener("keydown", event => {
  if (!event.target.matches(".posture-canvas")) return;
  const delta = {ArrowLeft: [-.01, 0], ArrowRight: [.01, 0], ArrowUp: [0, -.01], ArrowDown: [0, .01]}[event.key];
  if (!delta) return;
  event.preventDefault();
  const field = $("#location-kind").value, form = $("#review-form");
  const point = formPoint(form, field) || (form.elements[field].value !== "" ? regionCenter(Number(form.elements[field].value)) : [.5, .5]);
  choosePhotoPoint(point.map((n, i) => n + delta[i]));
});
$("#visit-dialog").addEventListener("change", event => {
  if (event.target.closest("#review-form")) {
    if (["region", "feces_region"].includes(event.target.name)) {
      if (event.target.value !== "" && $("#location-kind")) {
        chooseLocation(event.target.name, Number(event.target.value));
      } else {
        $("#review-form").elements[pointField(event.target.name)].value = "";
        updateLocationMarkers();
      }
    } else updateLocationMarkers();
  }
});
$("#close-cat-profile").onclick = () => $("#cat-profile-dialog").close();
$("#cat-profile-dialog").addEventListener("close", () => { selectedCat = null; });
$("#close-dialog").onclick = () => $("#visit-dialog").close();
$("#visit-dialog").addEventListener("click", (event) => {
  if (event.target === $("#visit-dialog")) {
    const r = event.target.getBoundingClientRect();
    if (
      event.clientX < r.left ||
      event.clientX > r.right ||
      event.clientY < r.top ||
      event.clientY > r.bottom
    )
      event.target.close();
  }
});
$("#visit-dialog").addEventListener("close", () => {
  selectedVisit = null;
});
$("#cat-filter").onchange = renderVisits;
$("#result-filter").onchange = renderVisits;
$("#notify-button").onclick = async () => {
  if (!("Notification" in window)) {
    toast(
      "Ta przeglądarka nie obsługuje powiadomień. Zdarzenia są dostępne w panelu.",
    );
    return;
  }
  const permission = await Notification.requestPermission();
  $("#notify-button").textContent =
    permission === "granted"
      ? "Powiadomienia włączone"
      : "Powiadomienia zablokowane";
  toast(
    permission === "granted"
      ? "Powiadomienia działają, gdy aplikacja pozostaje otwarta w przeglądarce."
      : "Zezwól na powiadomienia w ustawieniach przeglądarki.",
  );
};
if ("Notification" in window && Notification.permission === "granted")
  $("#notify-button").textContent = "Powiadomienia włączone";
setInterval(() => {
  if (!state || !connected) return;
  document.querySelectorAll("[data-duration]").forEach((el) => {
    const v = state.visits.find((v) => v.id === el.dataset.duration);
    if (v) el.textContent = duration(v);
  });
}, 1000);
const events = new EventSource("/api/events");
events.addEventListener("state", (event) => {
  try {
    acceptState(JSON.parse(event.data));
    setConnected(true);
  } catch (error) {
    console.error(error);
    setConnected(false);
  }
});
events.onerror = () => setConnected(false);

async function refreshCamera() {
  const preview = $("#camera-preview");
  try {
    if (document.hidden) return;
    const response = await fetch("/api/camera/status", { signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error("camera status");
    const status = await response.json();
    $("#camera-status").textContent = status.message;
    if (!status.online) {
      preview.hidden = true;
      preview.removeAttribute("src");
      return;
    }
    await new Promise((resolve) => {
      const finish = () => {
        clearTimeout(timer);
        preview.onload = preview.onerror = null;
        resolve();
      };
      const failed = () => {
        preview.hidden = true;
        preview.removeAttribute("src");
        $("#camera-status").textContent = "Nie udało się pobrać obrazu. Ponawianie…";
        finish();
      };
      const timer = setTimeout(failed, 5000);
      preview.onload = () => {
        preview.hidden = false;
        $("#camera-status").textContent = `Podgląd połączony · klatka ${new Date(status.captured_at).toLocaleTimeString("pl-PL")}`;
        finish();
      };
      preview.onerror = failed;
      preview.src = `/api/camera/frame.jpg?t=${Date.now()}`;
    });
  } catch {
    preview.hidden = true;
    $("#camera-status").textContent = "Brak połączenia z podglądem. Ponawianie…";
  } finally {
    setTimeout(refreshCamera, 1000);
  }
}
refreshCamera();

let savedRegions = [], draftRegions = [], markingBox = null, markingPoints = [], regionsLoaded = false;
const regionsSvg = $("#camera-regions");
function drawRegions() {
  regionsSvg.replaceChildren();
  const add = (tag, attrs, text) => {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
    if (text) el.textContent = text;
    regionsSvg.append(el);
  };
  const boxes = draftRegions.filter(box => box.id !== markingBox);
  if (markingBox && markingPoints.length) boxes.push({ id: markingBox, points: markingPoints });
  for (const box of boxes) {
    const points = box.points.map(([x, y]) => [x * 1000, y * 1000]);
    add(points.length === 4 ? "polygon" : "polyline", { points: points.map(p => p.join(",")).join(" "), class: `box-${box.id}` });
    add("text", { x: points[0][0], y: Math.max(28, points[0][1] - 12) }, `Kuweta 0${box.id}`);
    if (box.id === markingBox) for (const [cx, cy] of points) add("circle", { cx, cy, r: 5 });
  }
  regionsSvg.classList.toggle("editing", markingBox !== null);
  const dirty = JSON.stringify(savedRegions) !== JSON.stringify(draftRegions);
  $("#save-regions").disabled = !regionsLoaded || markingBox !== null || draftRegions.length !== 2 || !dirty;
  $("#cancel-regions").hidden = !dirty && markingBox === null;
}
for (const id of [1, 2]) $( `#mark-box-${id}` ).onclick = () => {
  if (!regionsLoaded || $("#camera-preview").hidden) {
    $("#regions-status").textContent = "Poczekaj na aktualny obraz i wczytanie ustawień.";
    return;
  }
  markingBox = id;
  markingPoints = [];
  $("#regions-status").textContent = `Kuweta 0${id}: kliknij kolejno 4 narożniki dookoła całej kuwety, zaczynając od lewego górnego.`;
  drawRegions();
};
regionsSvg.onclick = event => {
  if (markingBox === null || $("#camera-preview").hidden) return;
  const rect = regionsSvg.getBoundingClientRect();
  markingPoints.push([Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)), Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height))]);
  if (markingPoints.length === 4) {
    draftRegions = [...draftRegions.filter(box => box.id !== markingBox), { id: markingBox, points: markingPoints }].sort((a, b) => a.id - b.id);
    markingBox = null;
    $("#regions-status").textContent = "Obszar zaznaczony. Zaznacz drugą kuwetę, jeśli potrzebujesz, i zapisz obszary.";
  } else $("#regions-status").textContent = `Zaznaczono ${markingPoints.length}/4 narożniki.`;
  drawRegions();
};
$("#cancel-regions").onclick = () => {
  draftRegions = JSON.parse(JSON.stringify(savedRegions));
  markingBox = null;
  drawRegions();
  $("#regions-status").textContent = "Przywrócono zapisane obszary.";
};
$("#save-regions").onclick = async () => {
  $("#save-regions").disabled = true;
  try {
    const result = await post("/api/camera/regions", { boxes: draftRegions });
    savedRegions = result.boxes;
    draftRegions = JSON.parse(JSON.stringify(savedRegions));
    $("#regions-status").textContent = "Obszary zapisane. Nowe nagrania będą analizowane automatycznie; wyniki wymagają weryfikacji.";
  } catch (error) {
    $("#regions-status").textContent = error.message;
  } finally { drawRegions(); }
};
async function loadRegions() {
  try {
    const response = await fetch("/api/camera/regions", { signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error("Nie można wczytać obszarów.");
    const result = await response.json();
    savedRegions = result.boxes;
    draftRegions = JSON.parse(JSON.stringify(savedRegions));
    regionsLoaded = true;
    drawRegions();
    $("#regions-status").textContent = savedRegions.length ? "Wczytano zapisane obszary obu kuwet." : "Zaznacz obie kuwety na obrazie.";
  } catch {
    $("#regions-status").textContent = "Nie można wczytać obszarów. Ponawianie…";
    setTimeout(loadRegions, 5000);
  }
}
loadRegions();

$("#show-mock").addEventListener("change", () => { renderCats(); renderBoxes(); renderVisits(); });
