/* The backup catalog's file table - one implementation, injected into the server page
   (server-metrics.html, a server's own files) and the fleet page (backup-restore.html, every
   server's). It needs the page's `esc` and `when`. 0.34.0, 1.217; shared since the fleet page. */
/* ---------- Backup & restore files (0.34.0) ----------
   The backup catalog, file by file, newest first, in ONE table (the operator: backup and restore
   need not be two reports). A backup row carries what the backup wrote - its size in bytes, its
   SHA-256 recorded once and never changed, the engine's own verification, whether it is still on
   disk and still hashes to that value, the virus scan - and beside it every restore that used it:
   the mark (when, onto which target, the result), how many times, and whether a restore proved it.
   Files restored onto this server from another one are rows of their own.
   The filters are the questions a reader brings - "the FULLs", "what has a restore ever used" -
   so they hide rows already on the page rather than rebuilding it. */
const bytesB = n => n == null ? "—" : Number(n).toLocaleString("en-US") + " B";
const bytesHuman = n => {
  if (n == null) return "";
  const u = ["B","KB","MB","GB","TB"]; let v = Number(n), i = 0;
  while (v >= 1024 && i < u.length-1) { v /= 1024; i++; }
  return v.toFixed(i ? 2 : 0) + " " + u[i];
};
const shaCell = h => h ? `<span class="mono" title="${esc(h)}">${esc(h.slice(0,16))}…</span>`
  : `<span class="sev crit">none</span>`;
const tri = (v, yes, no) => v == null ? `<span class="sev">not checked</span>`
  : v ? `<span class="sev ok">${yes}</span>` : `<span class="sev crit">${no}</span>`;
const VERIFY_CLS = {passed:"ok", failed:"crit", not_run:""};
const COPY_VERDICT_CLS = {match:"ok", copied:"ok", recopied:"warn", copying:"warn"};
const RESULT_CLS = {done:"ok", copied:"ok", verified:"ok", refused:"crit", error:"crit"};
const KIND_LABEL = {backup:"backup", restored_here:"restored here", restore_out:"copied out"};
function virusCell(v){
  if (!v) return `<span class="sev">not scanned</span>`;
  const c = v.status === "clean" ? "ok" : v.status === "infected" ? "crit" : "warn";
  const about = [v.tool, v.version, v.signatures ? "signatures " + v.signatures : "",
                 v.by ? "by " + v.by : "", v.at ? "at " + v.at : "", v.text].filter(Boolean).join(" · ");
  return `<span class="sev ${c}" title="${esc(about)}">${esc(v.status)}</span>`;
}
/* Used for a restore: the file's mark, then the last copy's proof, then the tally. */
function restoreUseCell(f){
  if (!f.mark) return `<span class="sev">never used</span>`;
  const m = f.mark, r = f.lastRestore;
  const tested = r && r.testedOk ? `<span class="sev ok">RESTORE OK</span>`
    : m.action === "copy_only" ? `<span class="sev">copy only</span>`
    : `<span class="sev ${RESULT_CLS[m.result] || "warn"}">${esc(m.result || "")}</span>`;
  const copy = `<span class="sev ${COPY_VERDICT_CLS[m.verdict] || "crit"}">${esc(m.verdict || "")}</span>`;
  const proof = r ? `<div class="detail">target ${shaCell(r.sha256TargetAfter)} · ${
      tri(r.targetOnDisk, "on disk", "MISSING")} ${tri(r.targetMatches, "= catalog", "CHANGED")}${
      r.attempts != null ? ` · ${esc(r.attempts)} attempt(s), retry max ${esc(r.maxRetry)}` : ""}${
      r.refusedReason ? `<br>${esc(r.refusedReason)}` : ""}</div>` : "";
  return `${tested} ${copy}
    <div class="detail">${when(m.at)} · <span class="mono">${esc(m.action || "")}</span>
      ${f.kind === "restored_here" ? "from " + esc(f.server) : "→ " + esc(m.target || "")}${
      r && r.restoreId ? " · " + esc(r.restoreId) : ""}</div>${proof}
    <div class="detail">used ${esc(f.usedCount)}× · restored OK ${esc(f.restoredOk)}×</div>`;
}

/* Encrypted: what the backup script wrote the file with (its FILE line, 0.34.0) - "yes" / "no" -
   or "not recorded" for a file written before the scripts said, or catalogued by catalog-backfill:
   unknown is never shown as "no". A copy row has no answer of its own. */
function encryptedCell(f){
  if (f.kind !== "backup") return "";
  if (f.encrypted == null) return `<span class="sev">not recorded</span>`;
  return f.encrypted ? `<span class="sev ok">yes</span>` : `<span class="sev warn">no</span>`;
}

/* The Database filter's choices: the databases of the rows on the page, or of one server's rows
   when a server is picked - a database list across every server would offer names the picked
   server has never had. */
function dbOptions(files, server){
  const names = [...new Set((files || []).filter(f => !server || (f.host || "") === server)
    .map(f => f.database).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  return names.map(n => `<option>${esc(n)}</option>`).join("");
}

/* `opts.fleet`: the fleet page (backup-restore.html) - a Server column and filter, and the table
   always open; the server page folds it under its Backup section. */
/* Each backup entry's retention and point-in-time reach (0.35.0, 1.233), as its newest run recorded them. */
function retentionLine(rows){
  if (!rows || !rows.length) return "";
  return `<p class="sec-note">Retention: ${rows.map(r =>
    `<b>${esc(r.backupId)}</b> ${esc(r.windows)} · point-in-time reach <b>${esc(r.reach)}</b>`).join("; ")}</p>`;
}

function backupFilesBlock(b, opts = {}){
  if (!b || !b.files || !b.files.length) return "";
  const fleet = !!opts.fleet;
  const body = b.files.map(f=>{
    const bad = (f.kind === "backup" && f.verdict !== "ok") || f.checksumMatches === false
      || f.onDisk === false || (f.virus && f.virus.status === "infected")
      || (f.lastRestore && !f.lastRestore.proven);
    const use = !f.usedCount ? "never" : f.restoredOk ? "ok" : "used";
    const verdict = f.kind === "backup"
      ? `<span class="sev ${f.verdict === "ok" ? "ok" : "crit"}">${esc(f.verdict)}</span>
         <span class="sev ${VERIFY_CLS[f.engineVerify] ?? ""}" title="${esc(f.engineVerifyText)}">${esc(f.engineVerify)}</span>`
      : `<span class="sev">${esc(KIND_LABEL[f.kind])}</span>`;
    return `<tr data-type="${esc(f.type)}" data-use="${use}" data-server="${esc(f.host || "")}" data-db="${esc(f.database)}" data-bad="${bad ? "yes" : "no"}" class="${bad ? "crit" : ""}">
      <td class="nowrap">${when(f.at)}<div class="detail">${esc(KIND_LABEL[f.kind])}</div></td>
      ${fleet ? `<td class="nowrap mono">${esc(f.host || "")}</td>` : ""}
      <td>${esc(f.database)}</td>
      <td class="nowrap mono">${esc(f.type)}</td>
      <td class="metric">${esc(f.name)}<div class="detail mono">${esc(f.path)}${
        f.container ? " · container " + esc(f.container) : ""}</div></td>
      <td class="nowrap mono num" title="${esc(bytesHuman(f.sizeBytes))}">${f.sizeBytes == null
        ? `<span class="sev crit">unknown</span>` : esc(bytesB(f.sizeBytes))}</td>
      <td>${shaCell(f.sha256)}</td>
      <td>${verdict}</td>
      <td>${encryptedCell(f)}</td>
      <td>${tri(f.onDisk, "on disk", "MISSING")}</td>
      <td>${tri(f.checksumMatches, "= catalog", "CHANGED")}${f.lastChecked ? `<div class="detail">${when(f.lastChecked)}</div>` : ""}</td>
      <td>${virusCell(f.virus)}</td>
      <td>${restoreUseCell(f)}</td>
    </tr>`;
  }).join("");
  const problems = b.notOk + b.changed + b.missing + b.notProven + b.infected;
  const summary = [`${b.shown} file(s)`, b.notOk ? `${b.notOk} not ok` : "",
    b.changed ? `${b.changed} changed` : "", b.missing ? `${b.missing} missing` : "",
    b.infected ? `${b.infected} infected` : "", `${b.used} used for a restore`,
    `${b.restoredOk} restored OK`, b.notProven ? `${b.notProven} copy not proven` : ""]
    .filter(Boolean).join(" · ");
  const typeOpts = (b.types || []).map(t=>`<option>${esc(t)}</option>`).join("");
  const serverOpts = (b.servers || []).map(s=>`<option>${esc(s)}</option>`).join("");
  const note = fleet
    ? `Every file a backup of every server wrote, and every copy of a file no backup recorded,`
    : `Every file a backup of this server wrote,`;
  const head = fleet
    ? `<section class="files" id="backupFiles"><h2 class="sec">Backup &amp; restore files</h2>
       <p class="sec-note">${esc(summary)}</p><div>`
    : `<details class="fold" id="backupFiles"${problems ? " open" : ""}>
    <summary>Backup &amp; restore files — ${esc(summary)}</summary>
    <div class="in">`;
  return `${head}
    <p class="sec-note">${note} and every file a restore or a copy
      ${fleet ? "and every restore that used them," : "and every file a restore or a copy\n      carried to or from it,"} newest first, from the backup catalog. Size is in bytes as read when the
      file was written. SHA-256 is the checksum recorded then and never changed since; "= catalog"
      means the file's last check hashed to that same value. Each restore marks the files it used -
      when, onto which target, its result - and "RESTORE OK" means a restore copied this exact file
      intact, restored it and verified the databases.</p>
    ${retentionLine(b.retention)}
    <div class="ffilters">
      ${fleet ? `<label class="ffilter">Server <select class="ffsel" data-key="server">
        <option value="">All</option>${serverOpts}</select></label>` : ""}
      <label class="ffilter">Database <select class="ffsel" data-key="db" id="bf-db">
        <option value="">All</option>${dbOptions(b.files, "")}</select></label>
      <label class="ffilter">File type <select class="ffsel" data-key="type">
        <option value="">All</option>${typeOpts}</select></label>
      <label class="ffilter">Used for a restore <select class="ffsel" data-key="use">
        <option value="">All</option><option value="any">Used</option>
        <option value="ok">Restored OK</option><option value="used">Used, not restored OK</option>
        <option value="never">Never used</option></select></label>
      ${fleet ? `<label class="ffilter">Problems <select class="ffsel" data-key="bad">
        <option value="">All</option><option value="yes">Only problems</option></select></label>` : ""}
      <span class="sec-note">showing <b id="bf-count">${b.shown}</b> of ${b.shown}</span>
    </div>
    <div class="tbl-wrap"><table class="rpt">
      <thead><tr><th>When</th>${fleet ? "<th>Server</th>" : ""}<th>Database</th><th>Type</th><th>File / path on the server</th>
        <th>Size (B)</th><th>SHA-256</th><th>Verdict / engine verify</th><th>Encrypted</th><th>On disk</th>
        <th>Checksum</th><th>Virus scan</th><th>Used for restore</th></tr></thead>
      <tbody id="bf-rows">${body}</tbody></table></div></div>${fleet ? "</section>" : "</details>"}`;
}

/* The selects filter together, over the rows already rendered - an open section stays open. Picking
   a server narrows the Database choices to its own; a database it does not have is let go. */
function bindFileFilters(files){
  const sels = [...document.querySelectorAll("select.ffsel")];
  const server = sels.find(s => s.dataset.key === "server");
  const db = document.getElementById("bf-db");
  if (server && db && files) server.addEventListener("change", () => {
    const keep = db.value;
    db.innerHTML = `<option value="">All</option>` + dbOptions(files, server.value);
    db.value = [...db.options].some(o => o.value === keep) ? keep : "";
  });
  const apply = () => {
    const want = Object.fromEntries(sels.map(s=>[s.dataset.key, s.value]));
    let shown = 0;
    document.querySelectorAll("#bf-rows tr[data-type]").forEach(tr=>{
      // Every select at once; "use" has its one non-exact choice ("any" = used at all).
      const on = Object.entries(want).every(([key, value]) => !value
        || (key === "use" && value === "any" ? tr.dataset.use !== "never" : tr.dataset[key] === value));
      tr.style.display = on ? "" : "none";
      if (on) shown++;
    });
    const count = document.getElementById("bf-count");
    if (count) count.textContent = shown;
  };
  sels.forEach(s=>s.addEventListener("change", apply));
}
