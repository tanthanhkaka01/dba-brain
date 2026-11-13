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
  if (f.encrypted) return `<span class="sev ok">yes</span>`;
  // Not by the engine, but its repository copy is: no warning for a backup that is encrypted where it is kept.
  return f.repo && f.repo.total && f.repo.encrypted === f.repo.total
    ? `<span class="sev">no</span> <span class="text-secondary">- encrypted on the repository</span>`
    : `<span class="sev warn">no</span>`;
}

/* Repository (0.37.0): where the backup's copy is and what was proven of it - copied and verified,
   encrypted with which key and until when that key encrypts - or why not. One look per backup; its
   files, bytes and checksums before and after encryption are in the opened row. */
const bfDay = v => v ? esc(String(v).slice(0, 10)) : "";
function bfRepoCell(f){
  if (f.kind !== "backup") return "";
  const r = f.repo;
  if (!r) return `<span class="sev">not copied</span>`;
  const where = r.server ? `<div class="detail">→ ${esc(r.server)}</div>` : "";
  if (r.state === "verified") {
    const enc = !r.total ? ""
      : r.encrypted === r.total
        ? `<div class="detail"><span class="sev ok">encrypted ${r.encrypted}/${r.total}</span> key ${
            r.keys.map(k => "#" + esc(k)).join(", ")}</div>${r.keyUntil ? `<div class="detail">until ${bfDay(r.keyUntil)}</div>` : ""}`
        : r.encrypted ? `<div class="detail"><span class="sev warn">encrypted ${r.encrypted}/${r.total}</span></div>`
        : `<div class="detail"><span class="sev">not encrypted</span></div>`;
    return `<span class="sev ok">verified</span><div class="detail nowrap">${when(r.verifiedAt)}</div>${where}${enc}`;
  }
  if (r.state === "expired") return `<span class="sev">expired</span> ${when(r.expiredAt)}${where}`;
  if (r.state === "failed") return `<span class="sev crit">copy failed</span>${where}<div class="detail">${esc(r.error)}</div>`;
  return `<span class="sev warn">${esc(r.state || "waiting")}</span>${where}`;
}

/* On the server: still there (and still its bytes), deleted once its repository copy was verified,
   only the manifest the next incremental reads, or gone without a release. */
function bfOnServerCell(f){
  const s = f.onServer;
  if (!s) return tri(f.onDisk, "on disk", "MISSING");
  if (s.state === "released") return `<span class="sev ok">deleted</span><div class="detail">${when(s.at)}</div><div class="detail">after its copy was verified</div>`;
  if (s.state === "manifest_only") return `<span class="sev ok">manifest only</span><div class="detail">kept for the next incremental</div>`;
  if (s.state === "pruned") return `<span class="sev">pruned</span><div class="detail">${esc(s.by || "")}</div>`;
  if (s.state === "missing") return `<span class="sev crit">MISSING</span>`;
  // Where every backup of an entry on the old flow is, and should be: neutral, never a warning.
  return `<span class="sev">on the server</span><div class="detail">${tri(f.checksumMatches, "= catalog", "CHANGED")}</div>`;
}

/* Used for restore, in the row: its result and when; the proof opens with the row. */
function bfRestoreUseShort(f){
  if (!f.mark) return `<span class="sev">never used</span>`;
  const m = f.mark, r = f.lastRestore;
  const tested = r && r.testedOk ? `<span class="sev ok">RESTORE OK</span>`
    : m.action === "copy_only" ? `<span class="sev">copy only</span>`
    : `<span class="sev ${RESULT_CLS[m.result] || "warn"}">${esc(m.result || "")}</span>`;
  return `${tested}<div class="detail">${when(m.at)} → ${esc(m.target || "")}</div>
    <div class="detail">used ${esc(f.usedCount)}×</div>`;
}

/* The opened row: the backup's own facts in full, then each of its files from the server to the
   repository to its encrypted copy, then the restores that used it. */
/* Tabler's datagrid for the facts, its card table for the files, its badges for states. */
function bfFact(k, v){ return `<div class="datagrid-item"><div class="datagrid-title">${k}</div><div class="datagrid-content text-break">${v}</div></div>`; }
const bfSha = h => h ? `<div class="font-monospace text-secondary text-break small">${esc(h)}</div>` : "—";
/* On the server, in words - for the opened row, where the list's badge is out of sight (1.345: the
   operator read "plain removed on the repository" and could not tell whether the file itself was
   still on its server). The state is the backup row's: its files are released together. */
function bfOnServerSaid(f){
  const s = f.onServer;
  if (!s) return tri(f.onDisk, "on disk", "MISSING");
  if (s.state === "released") return `<span class="sev ok">deleted</span> ${when(s.at)} <span class="text-secondary">- after its repository copy was verified</span>`;
  if (s.state === "manifest_only") return `<span class="sev ok">manifest only</span> <span class="text-secondary">- kept for the next incremental</span>`;
  if (s.state === "pruned") return `<span class="sev">pruned</span> <span class="text-secondary">${esc(s.by || "")}</span>`;
  if (s.state === "missing") return `<span class="sev crit">MISSING</span>`;
  return `<span class="sev">still there</span> <span class="text-secondary">- not deleted</span>`;
}
function bfMemberRows(r, f){
  return r.files.map(m => {
    const a = m.age;
    const same = m.sha256 && m.repoSha256 && m.sha256 === m.repoSha256;
    return `<tr>
      <td class="mono">${esc(m.name)}</td>
      <td><div class="mono">${esc(bytesB(m.bytes))}</div>${bfSha(m.sha256)}<div class="mt-1">now: ${bfOnServerSaid(f)}</div></td>
      <td><div class="mono">${esc(bytesB(m.repoBytes))} ${same ? `<span class="sev ok">= server</span>` : `<span class="sev crit">differs</span>`}</div>
          ${bfSha(m.repoSha256)}${m.copiedAt ? `<div class="detail">copied ${when(m.copiedAt)}</div>` : ""}</td>
      <td>${a ? `<div class="mono">${esc(bytesB(a.bytes))}</div>${bfSha(a.sha256)}${
              a.encryptedAt ? `<div class="detail">encrypted ${when(a.encryptedAt)}</div>` : ""}` : `<span class="sev">not encrypted</span>`}</td>
      <td class="nowrap">${a ? `age key #${esc(a.keyId)}<div class="detail">encrypts until ${bfDay(a.keyUntil)}</div>
` : "—"}</td>
      <td>${a && a.provenAt ? `<span class="sev ok">decrypted = server</span><div class="detail">${when(a.provenAt)}</div>`
            : a ? `<span class="sev warn">not proven</span>` : "—"}</td>
      <td>${a && a.plainRemovedAt ? `<span class="sev ok">none</span><div class="detail">since ${when(a.plainRemovedAt)}</div>`
            : a ? `<span class="sev warn">still there</span>` : "—"}</td>
    </tr>`;
  }).join("");
}

/* The backup's life, one line per step, each with the moment the catalog recorded for it (1.345 - the
   operator: when was it written on the server, copied to the repository, encrypted, the plain copy
   gone there, deleted on the server?). A step that has not happened says so; none is guessed. A
   backup of several files reaches a step when its last file does. */
function bfTimeline(f){
  if (f.kind !== "backup") return "";
  const r = f.repo, s = f.onServer || {}, on = esc(f.server || "the server");
  const step = (what, at, note) => ({what, at, note});
  const not = text => `<span class="sev">${text}</span>`;
  const rows = [step(`Written on ${on}`, f.startedAt && f.startedAt !== f.at ? `${when(f.startedAt)} → ${when(f.at)}` : when(f.at),
                     "the backup began → ended")];
  if (!r) rows.push(step("Copied to the repository", not("not copied"), ""));
  else {
    rows.push(step(`Copied to ${esc(r.server || "the repository")}`,
      r.copiedAt ? (r.copyStartedAt && r.copyStartedAt !== r.copiedAt ? `${when(r.copyStartedAt)} → ${when(r.copiedAt)}` : when(r.copiedAt))
        : r.state === "failed" ? `<span class="sev crit">failed</span>` : r.verifiedAt ? not("not recorded") : not("not yet"),
      r.copiedAt ? "the copy began → ended" : r.verifiedAt ? "the copy's own start and end were not kept" : ""));
    rows.push(step("Copy verified", r.verifiedAt ? when(r.verifiedAt) : not("not yet"), "held to the server's bytes and SHA-256"));
    if (r.total) {
      rows.push(step("Encrypted there", r.encryptedAt ? when(r.encryptedAt) : not(r.encrypted ? `${r.encrypted} of ${r.total} file(s)` : "not encrypted"),
        r.keys.length ? `age key ${r.keys.map(k => "#" + esc(k)).join(", ")}` : ""));
      if (r.encrypted || r.encryptedAt) {
        rows.push(step("Encrypted copy proven", r.provenAt ? when(r.provenAt) : not("not yet"), "decrypted back to the server's bytes"));
        rows.push(step("No plain copy left on the repository", r.plainGoneAt ? when(r.plainGoneAt) : not("a plain file is still there"), ""));
      }
    }
    if (r.expiredAt) rows.push(step("Repository copy expired", when(r.expiredAt), "deleted there by its retention"));
  }
  rows.push(s.state === "released" ? step(`Deleted on ${on}`, when(s.at), "after its repository copy was verified")
    : s.state === "manifest_only" ? step(`Deleted on ${on}`, `<span class="sev ok">manifest only</span>`, "the manifest is kept for the next incremental")
    : s.state === "pruned" ? step(`Deleted on ${on}`, `<span class="sev">pruned</span>`, esc(s.by || ""))
    : s.state === "missing" ? step(`On ${on}`, `<span class="sev crit">MISSING</span>`, "gone without a release")
    : step(`Deleted on ${on}`, not("not deleted"), "the file is still there"));
  // Tabler's datagrid, like the facts above it - numbered, since the order is the point.
  return `<div class="subheader mt-3 mb-2">Timeline</div><div class="datagrid">${
    rows.map((x, n) => bfFact(`${n + 1}. ${x.what}`,
      `${x.at}${x.note ? ` <span class="text-secondary">- ${x.note}</span>` : ""}`)).join("")}</div>`;
}

/* A diff's or a log's restore chain (1.345 - the operator: for a diff or a log, can its full, diff and
   log chain be had?): the newest full before it, the differential or incrementals after that full and
   every log since - the files a restore to this one's end reads, in the catalog's order - and whether
   each can be had: its repository copy verified, or the file still on its server. */
function bfChainLink(l){
  const repo = l.repo === "verified" ? `<span class="sev ok">verified</span>${l.repoServer ? ` → ${esc(l.repoServer)}` : ""}`
    : l.repo ? `<span class="sev warn">${esc(l.repo)}</span>` : `<span class="sev">not copied</span>`;
  const server = l.onServer === "present" ? `<span class="sev">still there</span>`
    : l.onServer === "released" ? `<span class="sev ok">deleted</span> ${when(l.deletedAt)}`
    : l.onServer === "manifest_only" ? `<span class="sev ok">manifest only</span>`
    : l.onServer === "missing" ? `<span class="sev crit">MISSING</span>` : `<span class="sev">${esc(l.onServer || "")}</span>`;
  return `<tr><td>${esc(l.type)}</td><td class="mono">${esc(l.name)}</td><td class="nowrap">${when(l.at)}</td><td>${repo}</td><td>${server}</td></tr>`;
}
function bfChain(f){
  const c = f.chain;
  if (!c) return "";
  const head = `<div class="subheader mt-3 mb-2">Restore chain - the files a restore to the end of this one reads, in the catalog's order</div>`;
  if (!c.full) return `${head}<div><span class="sev ${c.selfGone ? "" : "warn"}">no chain</span> ${esc(c.note || "")}</div>`;
  const g = c.logs;
  const logs = g ? `<tr><td>LOG × ${esc(g.count)}</td><td>every log since, up to this file</td>
      <td class="nowrap">${when(g.from)} → ${when(g.to)}</td>
      <td><span class="sev ${g.onRepository === g.count ? "ok" : "warn"}">verified ${esc(g.onRepository)}/${esc(g.count)}</span></td>
      <td>${g.onServer ? `${esc(g.onServer)} of ${esc(g.count)} still there` : `none there`}</td></tr>` : "";
  const verdict = c.complete
    ? `<span class="sev ok">complete</span> ${c.onRepository ? "every file of it is verified on the repository" : "every file of it can be had - on the repository, or still on its server"}`
    : c.selfGone
      // The file itself is gone as its retention meant: its chain is history, said plainly and not in red.
      ? `<span class="sev">no longer restorable</span> this file is neither on the repository nor on the server, with ${
          esc(c.missingCount - 1)} other file(s) of its chain`
    : `<span class="sev crit">INCOMPLETE</span> ${esc(c.missingCount)} file(s) neither verified on the repository nor on the server: ${
        c.missing.map(esc).join(", ")}${c.missingCount > c.missing.length ? ", ..." : ""}`;
  return `${head}<div class="mb-2">${verdict}</div>
    <div class="card"><div class="table-responsive"><table class="table table-sm table-vcenter card-table"><thead><tr>
      <th>Level</th><th>File</th><th>Taken</th><th>Repository</th><th>On the server</th></tr></thead><tbody>
      ${bfChainLink(c.full)}${c.diffs.map(bfChainLink).join("")}${logs}</tbody></table></div></div>`;
}
function bfDetailRow(f, i, cols){
  const r = f.repo;
  // What is on the repository under its own name: an encrypted copy is `<file>.age`, not the plain path.
  const kept = r && r.files.length === 1 && r.files[0].age && r.files[0].age.path ? r.files[0].age.path : r ? r.path : "";
  const facts = [
    bfFact("Path on the server", `<span class="mono">${esc(f.path)}${f.container ? " · container " + esc(f.container) : ""}</span>`),
    f.kind === "backup" ? bfFact("On the server now", bfOnServerSaid(f)) : "",
    bfFact("Size", `<span class="mono">${esc(bytesB(f.sizeBytes))}</span> ${esc(bytesHuman(f.sizeBytes))}`),
    bfFact("SHA-256 when written", f.sha256 ? bfSha(f.sha256) : `<span class="sev crit">none</span>`),
    f.kind === "backup" ? bfFact("Engine verify", `<span class="sev ${VERIFY_CLS[f.engineVerify] ?? ""}">${esc(f.engineVerify)}</span> ${esc(f.engineVerifyText)}`) : "",
    f.kind === "backup" ? bfFact("Engine's own encryption", encryptedCell(f)) : "",
    bfFact("Last checked on the server", `${tri(f.checksumMatches, "= catalog", "CHANGED")} ${when(f.lastChecked)}`),
    bfFact("Virus scan", virusCell(f.virus)),
    r ? bfFact("Repository copy", `<span class="mono">${esc(r.server)}:${esc(kept)}</span>${
      r.files.length > 1 && r.total && r.encrypted === r.total ? ` <span class="text-secondary">- each file kept as .age</span>` : ""}`) : "",
  ].join("");
  const files = r && r.files.length ? `
    <div class="subheader mt-3 mb-2">${r.files.length} file(s) of this backup - on the server, on the repository, encrypted</div>
    <div class="card"><div class="table-responsive"><table class="table table-sm table-vcenter card-table"><thead><tr><th>File</th><th>On the server: bytes · SHA-256 · now</th>
      <th>Repository: bytes · SHA-256</th><th>Encrypted .age: bytes · SHA-256</th><th>Key</th>
      <th>Proven</th><th>Plain copy on the repository</th></tr></thead><tbody>${bfMemberRows(r, f)}</tbody></table></div></div>` : "";
  return `<tr class="more" id="bf-more-${i}" hidden><td colspan="${cols}">
    <div class="datagrid">${facts}</div>${bfTimeline(f)}${files}${bfChain(f)}
    <div class="subheader mt-3 mb-2">Restores</div><div>${restoreUseCell(f)}</div></td></tr>`;
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
  const body = b.files.map((f, i)=>{
    // Deleted once its repository copy was verified, or pruned by its retention: meant, not missing.
    const gone = f.onServer ? f.onServer.state === "missing" : f.onDisk === false;
    const bad = (f.kind === "backup" && f.verdict !== "ok") || f.checksumMatches === false
      || gone || (f.repo && f.repo.state === "failed") || (f.virus && f.virus.status === "infected")
      || (f.lastRestore && !f.lastRestore.proven);
    const use = !f.usedCount ? "never" : f.restoredOk ? "ok" : "used";
    const verdict = f.kind === "backup"
      ? `<span class="sev ${f.verdict === "ok" ? "ok" : "crit"}">${esc(f.verdict)}</span>
         <span class="sev ${VERIFY_CLS[f.engineVerify] ?? ""}" title="${esc(f.engineVerifyText)}">${esc(f.engineVerify)}</span>`
      : `<span class="sev">${esc(KIND_LABEL[f.kind])}</span>`;
    const count = f.repo && f.repo.total > 1 ? ` <span class="sev">${f.repo.total} files</span>` : "";
    const use_ = bfRestoreUseShort(f);
    const repo = f.kind !== "backup" ? "" : !f.repo ? "none" : f.repo.state === "verified" ? "verified" : "other";
    return `<tr class="bf-main ${bad ? "crit" : ""}" data-i="${i}" data-repo="${repo}" data-type="${esc(f.type)}" data-use="${use}" data-server="${esc(f.host || "")}" data-db="${esc(f.database)}" data-bad="${bad ? "yes" : "no"}">
      <td class="caret" aria-label="details">&#9656;</td>
      <td class="nowrap">${when(f.at)}<div class="detail">${esc(KIND_LABEL[f.kind])}</div></td>
      ${fleet ? `<td class="nowrap mono">${esc(f.host || "")}</td>` : ""}
      <td>${esc(f.database)}</td>
      <td class="nowrap mono">${esc(f.type)}</td>
      <td class="filecell" title="${esc(f.path)}"><b>${esc(f.name)}</b>${count}</td>
      <td class="nowrap mono num" title="${esc(bytesB(f.sizeBytes))}">${f.sizeBytes == null
        ? `<span class="sev crit">unknown</span>` : esc(bytesHuman(f.sizeBytes))}</td>
      <td>${verdict}</td>
      <td>${bfRepoCell(f)}</td>
      <td>${bfOnServerCell(f)}</td>
      <td>${use_}</td>
    </tr>${bfDetailRow(f, i, fleet ? 11 : 10)}`;
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
      means the file's last check hashed to that same value. Repository: the copy off the server,
      verified by its bytes and SHA-256, then encrypted with the server's age key and proven by
      decrypting it back to the same bytes. On the server: still there, or deleted once that copy was
      verified. Each restore marks the files it used - when, onto which target, its result - and
      "RESTORE OK" means a restore copied this exact file intact, restored it and verified the
      databases. Open a row for its files, sizes and checksums before and after encryption.</p>
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
      <label class="ffilter">Repository <select class="ffsel" data-key="repo">
        <option value="">All</option><option value="verified">On the repository</option>
        <option value="other">Copy pending or failed</option><option value="none">Not copied</option></select></label>
      ${fleet ? `<label class="ffilter">Problems <select class="ffsel" data-key="bad">
        <option value="">All</option><option value="yes">Only problems</option></select></label>` : ""}
      <span class="sec-note">showing <b id="bf-count">${b.shown}</b> of ${b.shown}</span>
    </div>
    <div class="tbl-wrap"><table class="rpt">
      <thead><tr><th></th><th>When</th>${fleet ? "<th>Server</th>" : ""}<th>Database</th><th>Type</th><th>File / path on the server</th>
        <th>Size</th><th>Verdict</th><th>Repository</th><th>On the server</th><th>Restore</th></tr></thead>
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
      const more = document.getElementById("bf-more-" + tr.dataset.i);
      if (more && !on) { more.hidden = true; tr.classList.remove("open"); tr.querySelector("td.caret").innerHTML = "&#9656;"; }
      if (on) shown++;
    });
    const count = document.getElementById("bf-count");
    if (count) count.textContent = shown;
  };
  sels.forEach(s=>s.addEventListener("change", apply));
  const rows = document.getElementById("bf-rows");
  if (rows) rows.addEventListener("click", e => {
    if (e.target.closest("a, select, button, tr.more")) return;
    const tr = e.target.closest("tr.bf-main");
    const more = tr && document.getElementById("bf-more-" + tr.dataset.i);
    if (!more) return;
    more.hidden = !more.hidden;
    tr.classList.toggle("open", !more.hidden);
    tr.querySelector("td.caret").innerHTML = more.hidden ? "&#9656;" : "&#9662;";
  });
}
