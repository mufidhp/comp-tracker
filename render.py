"""
render.py — build a self-contained index.html from data.json.

Design constraints (from spec + audit):
  * Everything inline: CSS + JS + the data itself (JSON is embedded, NOT fetched),
    so the page works when opened from disk (file://) and on GitHub Pages.
  * All scraped text is rendered via textContent (never innerHTML) and link hrefs
    are sanitized to http(s) only — scraped titles can't run code in the page,
    which is what protects the GitHub token held in sessionStorage.
  * A strict CSP blocks every external origin except api.github.com (needed only
    for the manual smart-scan dispatch).
  * Countdowns are computed in the browser and refresh every minute.
"""
from __future__ import annotations

import json


def render(data: dict, cfg: dict) -> str:
    th = cfg.get("thresholds", {})
    consts = {
        "amberHours": th.get("amber_hours", 72),
        "redHours": th.get("ending_soon_hours", 24),
        "chipHours": th.get("ending_chip_hours", 48),
        "smartConfigured": bool(data.get("smart_configured", False)),
        "models": [
            {"id": "haiku", "label": "Haiku (~$0.05–0.15/scan)"},
            {"id": "sonnet", "label": "Sonnet (~$0.30–0.80/scan, smarter)"},
        ],
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    consts_json = json.dumps(consts, ensure_ascii=False).replace("</", "<\\/")

    return _TEMPLATE.replace("/*DATA*/", payload).replace("/*CONSTS*/", consts_json)


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src https://api.github.com; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'">
<title>Crypto Spot Competition Tracker</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --border:#2a3240;
    --text:#e6edf3; --muted:#9aa7b4; --accent:#4c9aff;
    --green:#2ea043; --amber:#d29922; --red:#e5534b; --grey:#6e7681;
    --avoid:#3d1418; --avoidbd:#e5534b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
       line-height:1.45;font-size:15px}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  header{padding:18px 20px;border-bottom:1px solid var(--border);background:var(--panel)}
  header.stale{background:#3d1418;border-bottom-color:var(--avoidbd)}
  h1{margin:0 0 4px;font-size:20px}
  .sub{color:var(--muted);font-size:13px}
  .wrap{max-width:1100px;margin:0 auto;padding:16px 20px 60px}
  .chips{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}
  .chip{background:var(--panel2);border:1px solid var(--border);color:var(--text);
        padding:6px 12px;border-radius:20px;cursor:pointer;font-size:13px;user-select:none}
  .chip[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:#fff}
  .health{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0 4px}
  .hpill{font-size:11px;padding:3px 8px;border-radius:6px;border:1px solid var(--border);
         background:var(--panel2);color:var(--muted)}
  .hpill.ok{color:#7ee2a8} .hpill.stale{color:var(--amber)}
  .hpill.blocked,.hpill.failed{color:var(--red)} .hpill.empty{color:var(--amber)}
  .hpill.disabled{opacity:.5}
  .smartbar{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:16px 0;
            padding:12px 14px;background:var(--panel);border:1px solid var(--border);border-radius:10px}
  .smartbar select,.smartbar input,.smartbar button{
    background:var(--panel2);color:var(--text);border:1px solid var(--border);
    border-radius:8px;padding:8px 10px;font-size:13px}
  .smartbar button{cursor:pointer;background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  .smartbar button:disabled{background:var(--panel2);color:var(--muted);cursor:not-allowed;border-color:var(--border)}
  .smallnote{font-size:12px;color:var(--muted)}
  details.settings{margin-left:auto}
  details.settings summary{cursor:pointer;color:var(--muted);font-size:12px}
  .setrow{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
  .groupttl{margin:22px 0 8px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 15px;position:relative}
  .card.avoid{background:var(--avoid);border-color:var(--avoidbd)}
  .card h3{margin:0 0 6px;font-size:15px;line-height:1.3;padding-right:70px}
  .badges{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
  .b{font-size:11px;padding:2px 7px;border-radius:6px;border:1px solid var(--border)}
  .b.spot{color:#7ee2a8} .b.onchain{color:#79c0ff} .b.mixed{color:#e3b341}
  .b.tierA{color:#7ee2a8;border-color:#2ea04366} .b.caution{color:#e3b341}
  .b.avoid{color:#fff;background:var(--avoidbd);border-color:var(--avoidbd)}
  .b.new{color:#fff;background:var(--accent);border-color:var(--accent)}
  .cd{position:absolute;top:12px;right:12px;font-size:12px;font-weight:700;padding:3px 8px;border-radius:6px}
  .cd.green{color:#7ee2a8;background:#1c3a24} .cd.amber{color:#f0c674;background:#3a2f14}
  .cd.red{color:#ffb3ae;background:#3d1418} .cd.grey{color:var(--muted);background:var(--panel2)}
  .meta{font-size:13px;color:var(--muted);margin:4px 0}
  .meta b{color:var(--text);font-weight:600}
  .warn{color:var(--amber);font-size:12px;margin-top:4px}
  .note{margin-top:8px;padding:8px;background:var(--panel2);border-radius:8px;font-size:13px;color:#cdd7e0}
  .open{display:inline-block;margin-top:10px;font-size:13px;border:1px solid var(--accent);
        padding:5px 10px;border-radius:8px}
  .empty{color:var(--muted);padding:30px;text-align:center}
  footer{color:var(--muted);font-size:12px;text-align:center;padding:24px}
  @media (max-width:520px){.cards{grid-template-columns:1fr}}
</style>
</head>
<body>
<header id="hdr">
  <h1>🏆 Crypto Spot Competition Tracker</h1>
  <div class="sub" id="subline"></div>
</header>
<div class="wrap">
  <div class="smartbar">
    <span class="smallnote">Auto scans are <b>free &amp; rule-based</b>. Smart scan uses AI (your chosen model) and runs only when you click.</span>
    <select id="model" aria-label="AI model"></select>
    <button id="runSmart">Run smart scan</button>
    <span class="smallnote" id="smartStatus"></span>
    <details class="settings">
      <summary>⚙ smart-scan setup</summary>
      <div class="setrow">
        <input id="repo" placeholder="owner/repo (e.g. shaista/comp-tracker)" size="28">
        <input id="token" type="password" placeholder="GitHub fine-grained token (actions:write)" size="34">
        <button id="saveTok">Save for this session</button>
      </div>
      <div class="smallnote" style="margin-top:6px">
        Token is kept only in this browser tab (sessionStorage) and used only to start the smart-scan workflow. Closing the tab forgets it.
      </div>
    </details>
  </div>

  <div class="chips" id="chips"></div>
  <div class="health" id="health"></div>
  <div id="groups"></div>
</div>
<footer>Times shown in Pakistan time (PKT, UTC+5). Auto scans run twice daily &amp; are free; smart scan is optional and paid.</footer>

<script id="appdata" type="application/json">/*DATA*/</script>
<script id="appconsts" type="application/json">/*CONSTS*/</script>
<script>
(function(){
  "use strict";
  var DATA = JSON.parse(document.getElementById("appdata").textContent);
  var C = JSON.parse(document.getElementById("appconsts").textContent);
  var comps = (DATA.competitions||[]).slice();

  function el(tag, cls){ var e=document.createElement(tag); if(cls) e.className=cls; return e; }
  function txt(tag, cls, s){ var e=el(tag,cls); e.textContent=(s==null?"":String(s)); return e; }
  function safeHref(u){ u=String(u||""); return /^https?:\/\//i.test(u)? u : "#"; }

  function fmtPKT(iso){
    if(!iso) return "date TBD";
    var d=new Date(iso); if(isNaN(d)) return "date TBD";
    // Force UTC+5 display regardless of viewer's own zone.
    var pk=new Date(d.getTime()+5*3600*1000);
    var mon=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][pk.getUTCMonth()];
    var h=pk.getUTCHours(), m=pk.getUTCMinutes();
    var ap=h>=12?"PM":"AM"; var h12=h%12; if(h12===0) h12=12;
    return pk.getUTCDate()+" "+mon+", "+h12+":"+(m<10?"0":"")+m+" "+ap+" PKT";
  }
  function hoursLeft(iso){ if(!iso) return null; var d=new Date(iso); if(isNaN(d)) return null;
    return (d.getTime()-Date.now())/3600000; }
  function countdown(iso){
    var hl=hoursLeft(iso);
    if(hl===null) return {cls:"grey",label:"dates TBD"};
    if(hl<=0) return {cls:"grey",label:"ENDED"};
    var cls = hl<=C.redHours?"red":(hl<=C.amberHours?"amber":"green");
    var d=Math.floor(hl/24), h=Math.floor(hl%24), mm=Math.floor((hl*60)%60);
    var label = d>0? (d+"d "+h+"h") : (h>0? (h+"h "+mm+"m") : (mm+"m"));
    return {cls:cls,label:label+" left"};
  }

  // ---- filters ----
  var FILTERS=[
    {id:"all",label:"All"},
    {id:"soon",label:"Ending <"+C.chipHours+"h"},
    {id:"onchain",label:"Onchain"},
    {id:"cex",label:"CEX spot"},
    {id:"safe",label:"Safe (Tier-A)"},
    {id:"new",label:"New"}
  ];
  var active="all";
  function pass(c){
    if(active==="all") return true;
    if(active==="soon"){var hl=hoursLeft(c.end_utc); return hl!==null&&hl>0&&hl<=C.chipHours;}
    if(active==="onchain") return c.type==="onchain";
    if(active==="cex") return c.type==="spot"||c.type==="mixed";
    if(active==="safe") return c.tier==="A";
    if(active==="new") return !!c.is_new;
    return true;
  }

  function badge(cls,label){ return txt("span","b "+cls,label); }

  function card(c){
    var box=el("div","card"+(c.tier==="avoid"?" avoid":""));
    var cd=countdown(c.end_utc);
    var cdEl=txt("span","cd "+cd.cls,cd.label); cdEl.setAttribute("data-end",c.end_utc||"");
    box.appendChild(cdEl);
    box.appendChild(txt("h3",null,c.name));

    var badges=el("div","badges");
    if(c.is_new) badges.appendChild(badge("new","NEW"));
    if(c.type) badges.appendChild(badge(c.type,c.type.toUpperCase()));
    var tierCls=c.tier==="A"?"tierA":(c.tier==="avoid"?"avoid":"caution");
    var tierLbl=c.tier==="A"?"TIER-A SAFE":(c.tier==="avoid"?"⚠ AVOID":"CAUTION");
    badges.appendChild(badge(tierCls,tierLbl));
    box.appendChild(badges);

    var venue=txt("div","meta",null); venue.appendChild(txt("b",null,c.venue||"?"));
    if(c.prize){ venue.appendChild(document.createTextNode(" · "+c.prize)); }
    box.appendChild(venue);

    var period=el("div","meta");
    period.appendChild(document.createTextNode("Runs: "+fmtPKT(c.start_utc)+" → "+fmtPKT(c.end_utc)));
    box.appendChild(period);

    [["Structure",c.structure],["Entry",c.entry],["Eligibility",c.eligibility],["Fee",c.fee]]
      .forEach(function(kv){ if(kv[1]){ var m=el("div","meta");
        m.appendChild(txt("b",null,kv[0]+": ")); m.appendChild(document.createTextNode(kv[1])); box.appendChild(m);} });

    if(c.end_utc && c.date_confidence!=="confirmed")
      box.appendChild(txt("div","warn","⚠ verify dates on official page"));
    if(!c.end_utc)
      box.appendChild(txt("div","warn","⚠ end date unknown — run smart scan or check official page"));

    if(c.note){ box.appendChild(txt("div","note","🧠 "+c.note)); }

    var a=el("a","open"); a.textContent="Open announcement";
    a.href=safeHref(c.official_link); a.target="_blank"; a.rel="noopener noreferrer";
    box.appendChild(a);
    return box;
  }

  function render(){
    var groups=document.getElementById("groups"); groups.textContent="";
    var order=[["A","Tier-A · safe"],["caution","Caution"],["avoid","⚠ Avoid (not recommended)"]];
    var shown=0;
    order.forEach(function(g){
      var list=comps.filter(function(c){return c.tier===g[0] && pass(c);});
      // sort: live first, soonest end first, then unknown-date
      list.sort(function(a,b){
        var ha=hoursLeft(a.end_utc), hb=hoursLeft(b.end_utc);
        var aa=(ha===null||ha<=0), bb=(hb===null||hb<=0);
        if(aa!==bb) return aa?1:-1;
        if(ha===null) return 1; if(hb===null) return -1;
        return ha-hb;
      });
      if(!list.length) return;
      groups.appendChild(txt("div","groupttl",g[1]+" ("+list.length+")"));
      var grid=el("div","cards");
      list.forEach(function(c){ grid.appendChild(card(c)); shown+=1; });
      groups.appendChild(grid);
    });
    if(!shown){ var e=txt("div","empty","No competitions match this filter."); groups.appendChild(e); }
  }

  // ---- header + health ----
  function head(){
    var sub=document.getElementById("subline");
    var gen=DATA.generated_utc? fmtPKT(DATA.generated_utc):"—";
    var mode=DATA.last_mode==="B"?"B (smart / AI)":"A (auto / free)";
    sub.textContent="Last updated "+gen+"  ·  last mode: "+mode+"  ·  "+comps.length+" competitions tracked";
    var hl=hoursLeft(DATA.generated_utc);
    if(hl!==null && hl < -26){ document.getElementById("hdr").className="stale";
      sub.textContent+="  ·  ⚠ data looks stale (>26h old)"; }
  }
  function health(){
    var box=document.getElementById("health"); box.textContent="";
    (DATA.source_health||[]).forEach(function(h){
      var p=txt("span","hpill "+(h.status||""), h.source+": "+(h.status||"?"));
      if(h.error) p.title=h.error;
      box.appendChild(p);
    });
  }
  function chips(){
    var box=document.getElementById("chips"); box.textContent="";
    FILTERS.forEach(function(f){
      var c=txt("span","chip",f.label);
      c.setAttribute("aria-pressed", f.id===active?"true":"false");
      c.setAttribute("role","button"); c.tabIndex=0;
      function go(){ active=f.id; chips(); render(); }
      c.addEventListener("click",go);
      c.addEventListener("keydown",function(e){ if(e.key==="Enter"||e.key===" "){e.preventDefault();go();} });
      box.appendChild(c);
    });
  }

  // ---- live countdown refresh (every minute) ----
  function tick(){
    document.querySelectorAll(".cd").forEach(function(elm){
      var iso=elm.getAttribute("data-end");
      var cd=countdown(iso||null);
      elm.className="cd "+cd.cls; elm.textContent=cd.label;
    });
  }

  // ---- smart scan control ----
  function initModels(){
    var sel=document.getElementById("model");
    C.models.forEach(function(m){ var o=el("option"); o.value=m.id; o.textContent=m.label; sel.appendChild(o); });
    var repo=document.getElementById("repo"), token=document.getElementById("token");
    repo.value=sessionStorage.getItem("ct_repo")||"";
    token.value=sessionStorage.getItem("ct_token")?"":""; // never prefill secret text
    document.getElementById("saveTok").addEventListener("click",function(){
      sessionStorage.setItem("ct_repo",repo.value.trim());
      if(token.value.trim()) sessionStorage.setItem("ct_token",token.value.trim());
      token.value="";
      status("Saved for this browser session.");
    });
    var btn=document.getElementById("runSmart");
    if(!C.smartConfigured){ btn.disabled=true; btn.title="add ANTHROPIC_API_KEY as a GitHub secret to enable";
      status("smart mode not configured"); }
    btn.addEventListener("click",runSmart);
  }
  function status(s){ document.getElementById("smartStatus").textContent=s; }
  function runSmart(){
    var repo=sessionStorage.getItem("ct_repo"), token=sessionStorage.getItem("ct_token");
    var model=document.getElementById("model").value;
    if(!repo||!token){ status("Open ⚙ setup and save your owner/repo + token first."); return; }
    if(!/^[\w.-]+\/[\w.-]+$/.test(repo)){ status("Repo must look like owner/repo."); return; }
    status("Starting smart scan ("+model+")…");
    fetch("https://api.github.com/repos/"+repo+"/actions/workflows/smart-scan.yml/dispatches",{
      method:"POST",
      headers:{"Accept":"application/vnd.github+json","Authorization":"Bearer "+token,
               "X-GitHub-Api-Version":"2022-11-28"},
      body:JSON.stringify({ref:"main",inputs:{model:model}})
    }).then(function(r){
      if(r.status===204){ status("✅ Smart scan started on GitHub. It commits results in ~1–3 min; refresh the page after."); }
      else if(r.status===401||r.status===403){ status("❌ Token rejected or expired. Make a new fine-grained token (actions:write) and save it again."); sessionStorage.removeItem("ct_token"); }
      else if(r.status===404){ status("❌ Workflow or repo not found. Check owner/repo and that smart-scan.yml is on the main branch."); }
      else { r.text().then(function(t){ status("❌ GitHub said "+r.status+": "+t.slice(0,120)); }); }
    }).catch(function(e){ status("❌ Network error: "+e.message); });
  }

  // init
  comps.forEach(function(c){ if(c.hours_left===undefined) c.hours_left=hoursLeft(c.end_utc); });
  head(); health(); chips(); render(); initModels();
  setInterval(tick,60000);
})();
</script>
</body>
</html>
"""
