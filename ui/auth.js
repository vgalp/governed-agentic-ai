// Sign-in for the browser pages (ADR 028). Load it before a page's own script.
//
// Demo mode (the default): does nothing; the pages work as before, with their role menu.
// oidc mode: signs the person in with the organization's identity provider (authorization code
// flow with PKCE, a public client: no secret in the page), then:
//   - adds "Authorization: Bearer <token>" to the page's own API calls (fetch is wrapped);
//   - GovAuth.eventSource(url) replaces new EventSource(url), which cannot send that header;
//   - shows who is signed in, their role, and "Sign out"; the pages' role menus are hidden,
//     since the role now comes from the person's groups.
// The token is kept in this tab only (sessionStorage) and renewed before it expires.
(() => {
  const KEY = "govauth";
  const nativeFetch = window.fetch.bind(window);
  const OPEN = ["/auth/config", "/auth.js", "/profile", "/health", "/audit/verify", "/evaluations"];
  let cfg = { mode: "demo" }, oidc = null;

  const b64url = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const random = n => b64url(crypto.getRandomValues(new Uint8Array(n)));
  const load = () => { try { return JSON.parse(sessionStorage.getItem(KEY) || "null"); } catch { return null; } };
  const save = v => { try { v ? sessionStorage.setItem(KEY, JSON.stringify(v)) : sessionStorage.removeItem(KEY); } catch { /* private window */ } };
  const here = () => location.origin + location.pathname;

  async function discover() {
    if (!oidc) {
      const r = await nativeFetch(cfg.issuer.replace(/\/$/, "") + "/.well-known/openid-configuration");
      if (!r.ok) throw new Error("The sign-in service did not answer (" + r.status + ").");
      oidc = await r.json();
    }
    return oidc;
  }

  async function signIn() {
    const d = await discover();
    const verifier = random(48), state = random(16);
    const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
    sessionStorage.setItem(KEY + ":pkce", JSON.stringify({ verifier, state, back: location.href }));
    const q = new URLSearchParams({ response_type: "code", client_id: cfg.client_id, redirect_uri: here(),
      scope: cfg.scope || "openid", state, code_challenge: challenge, code_challenge_method: "S256" });
    location.assign(d.authorization_endpoint + "?" + q);
    return new Promise(() => {});                       // the page is leaving
  }

  async function token(body) {
    const d = await discover();
    const r = await nativeFetch(d.token_endpoint, { method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(body) });
    if (!r.ok) return null;
    const t = await r.json();
    if (!t.access_token) return null;
    return { access: t.access_token, refresh: t.refresh_token || null, id: t.id_token || null,
             expires: Date.now() + 1000 * (t.expires_in || 300) };
  }

  async function finishSignIn(params) {
    let pkce = null;
    try { pkce = JSON.parse(sessionStorage.getItem(KEY + ":pkce") || "null"); } catch { /* none */ }
    sessionStorage.removeItem(KEY + ":pkce");
    if (!pkce || params.get("state") !== pkce.state) throw new Error("The sign-in reply did not match this page. Try again.");
    const t = await token({ grant_type: "authorization_code", code: params.get("code"), redirect_uri: here(),
                            client_id: cfg.client_id, code_verifier: pkce.verifier });
    if (!t) throw new Error("The sign-in service refused the sign-in. Try again.");
    save(t);
    history.replaceState(null, "", pkce.back && new URL(pkce.back).pathname === location.pathname ? pkce.back : here());
  }

  async function current() {
    let t = load();
    if (t && t.expires - Date.now() < 30000) {         // renew shortly before it expires
      t = t.refresh ? await token({ grant_type: "refresh_token", refresh_token: t.refresh, client_id: cfg.client_id }) : null;
      save(t);
    }
    return t;
  }

  const ready = (async () => {
    try { cfg = await (await nativeFetch("/auth/config")).json(); } catch { cfg = { mode: "demo" }; }
    if (cfg.mode !== "oidc") return cfg;
    document.documentElement.dataset.signin = "oidc";
    const params = new URLSearchParams(location.search);
    if (params.get("error")) throw new Error("Sign-in failed: " + (params.get("error_description") || params.get("error")));
    if (params.get("code") && params.get("state")) await finishSignIn(params);
    if (!(await current())) await signIn();
    return cfg;
  })();

  const protectedPath = input => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    return url.origin === location.origin && !OPEN.includes(url.pathname);
  };

  window.fetch = async (input, init = {}) => {
    if (!protectedPath(input)) return nativeFetch(input, init);
    await ready;
    if (cfg.mode !== "oidc") return nativeFetch(input, init);
    const t = await current();
    if (!t) return signIn();
    const headers = new Headers(init.headers || (typeof input !== "string" ? input.headers : undefined));
    headers.set("Authorization", "Bearer " + t.access);
    const r = await nativeFetch(input, { ...init, headers });
    if (r.status === 401) { save(null); return signIn(); }
    return r;
  };

  // Server-sent events over fetch, so the token can go in a header. Same interface as EventSource.
  function streamEvents(url, es) {
    const ctl = new AbortController();
    es.close = () => { es.closed = true; ctl.abort(); };
    let lastId = 0;
    (async function run() {
      while (!es.closed) {
        try {
          const r = await window.fetch(url, { headers: lastId ? { "Last-Event-ID": String(lastId) } : {}, signal: ctl.signal });
          if (!r.ok || !r.body) throw new Error("stream " + r.status);
          es.onopen && es.onopen();
          const reader = r.body.getReader(), dec = new TextDecoder();
          let buf = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            let cut;
            while ((cut = buf.indexOf("\n\n")) >= 0) {
              const block = buf.slice(0, cut); buf = buf.slice(cut + 2);
              let data = [], id = null;
              for (const line of block.split("\n")) {
                if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
                else if (line.startsWith("id:")) id = line.slice(3).trim();
              }
              if (id) lastId = Number(id) || lastId;
              if (data.length && es.onmessage) es.onmessage({ data: data.join("\n"), lastEventId: id });
            }
          }
        } catch (e) { if (es.closed) return; }
        es.onerror && es.onerror();
        await new Promise(res => setTimeout(res, 3000));   // reconnect, like EventSource
      }
    })();
    return es;
  }

  function signOut() {
    const t = load();
    save(null);
    discover().then(d => {
      if (!d.end_session_endpoint) return location.reload();
      const q = new URLSearchParams({ client_id: cfg.client_id, post_logout_redirect_uri: here() });
      if (t && t.id) q.set("id_token_hint", t.id);
      location.assign(d.end_session_endpoint + "?" + q);
    }).catch(() => location.reload());
  }

  async function showBadge() {
    if (cfg.mode !== "oidc") return;
    const style = document.createElement("style");
    style.textContent = `html[data-signin=oidc] #role, html[data-signin=oidc] .demo-role { display: none !important; }
      #govauth-bar { position: fixed; top: 8px; right: 12px; z-index: 1000; font: 13px system-ui, sans-serif;
        background: #fff; color: #222; border: 1px solid #ccd; border-radius: 8px; padding: 4px 10px; }
      #govauth-bar button { margin-left: 8px; font: inherit; cursor: pointer; }
      #govauth-bar .warn { color: #a40; }`;
    document.head.appendChild(style);
    const bar = document.createElement("div");
    bar.id = "govauth-bar";
    try {
      const me = await (await window.fetch("/auth/me")).json();
      bar.textContent = "Signed in: " + (me.name || me.user) + " · ";
      const role = document.createElement("span");
      role.textContent = me.role_title || "no role in this assistant";
      if (!me.role) role.className = "warn";
      bar.appendChild(role);
    } catch { bar.textContent = "Signed in"; }
    const out = document.createElement("button");
    out.textContent = "Sign out";
    out.onclick = signOut;
    bar.appendChild(out);
    document.body.appendChild(bar);
  }

  ready.then(() => {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", showBadge);
    else showBadge();
  }).catch(e => {
    document.addEventListener("DOMContentLoaded", () => {
      const p = document.createElement("p");
      p.style.cssText = "position:fixed;top:8px;left:50%;transform:translateX(-50%);z-index:1000;background:#fee;" +
                        "color:#600;border:1px solid #c99;padding:8px 12px;border-radius:8px;font:14px system-ui";
      p.textContent = e.message;
      document.body.appendChild(p);
    });
  });

  window.GovAuth = {
    ready,
    signOut,
    // Returns at once (pages set onopen/onmessage/onerror on it); connects when sign-in is settled.
    eventSource(url) {
      const es = { onopen: null, onmessage: null, onerror: null, closed: false, close() { this.closed = true; } };
      ready.then(() => {
        if (es.closed) return;
        if (cfg.mode === "oidc") return streamEvents(url, es);
        const native = new EventSource(url);
        native.onopen = e => es.onopen && es.onopen(e);
        native.onmessage = e => es.onmessage && es.onmessage(e);
        native.onerror = e => es.onerror && es.onerror(e);
        es.close = () => { es.closed = true; native.close(); };
      }).catch(() => es.onerror && es.onerror());
      return es;
    },
  };
})();
