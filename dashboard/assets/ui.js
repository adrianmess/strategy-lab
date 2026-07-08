/* Shared UI helpers: theme toggle + strategy lineage names + css var reader. */
(function () {
  const saved = localStorage.getItem("theme");
  if (saved === "light") document.documentElement.dataset.theme = "light";
  window.addEventListener("DOMContentLoaded", () => {
    // The Control panel lives on the panel server (port 8800). If this page
    // was opened any other way (file://, another static server), rewrite the
    // link to the absolute panel URL so it always works.
    if (location.port !== "8800") {
      document.querySelectorAll('a[href="/"]').forEach(a => {
        a.href = "http://127.0.0.1:8800/";
        a.title = "Opens the control panel server (start it with: python3 panel/server.py)";
      });
    }
    const nav = document.querySelector("header nav");
    if (!nav) return;
    const b = document.createElement("button");
    b.id = "themeToggle";
    b.textContent = document.documentElement.dataset.theme === "light" ? "☾ dark" : "☀ light";
    b.onclick = () => {
      const light = document.documentElement.dataset.theme === "light";
      document.documentElement.dataset.theme = light ? "" : "light";
      localStorage.setItem("theme", light ? "dark" : "light");
      b.textContent = light ? "☀ light" : "☾ dark";
      if (window.onThemeChange) window.onThemeChange();
    };
    nav.appendChild(b);
  });
})();

/* internal id -> human name showing which original strategy it came from */
window.STRAT_NAMES = {
  v7: "V5 family · V7 full-param",
  v6: "V5 family · V6",
  "v5.2": "V5 family · V5.2",
  v5: "V5 (original)",
  prime: "V5 family · Solana Prime",
  prime7: "V5 family · Prime7 (Prime, full-param)",
  scalpx: "Scalp family · ScalpX",
  scalpx2: "Scalp family · ScalpX2 (full-param)",
  scalp: "Scalp (original)",
};
window.stratName = s => window.STRAT_NAMES[(s || "").toLowerCase()] || s || "unknown";
window.cssv = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// stale-server banner: the panel server keeps running old code until restarted
fetch('/api/version').then(r=>r.json()).then(v=>{
  if(!v.stale) return;
  const b = document.createElement('div');
  b.style.cssText = 'position:sticky;top:0;z-index:99;background:var(--red,#d33);color:#fff;'+
    'padding:8px 20px;font:600 13px -apple-system,sans-serif;text-align:center';
  b.textContent = '⚠ The panel server is running an OLDER version of the code — new features are silently ignored. '+
    'Stop it (Ctrl+C in its terminal) and start it again:  python3 panel/server.py';
  document.body.prepend(b);
}).catch(()=>{});
