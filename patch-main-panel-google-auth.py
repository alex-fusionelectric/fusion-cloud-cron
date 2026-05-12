"""patch-main-panel-google-auth.py — Idempotently patches
fusion-bid-list/site/index.html to use Google OAuth + per-user
panel access. Run-and-deploy in one shot so OneDrive sync has no
window to revert the file before Netlify uploads it.

Usage:
    python patch-main-panel-google-auth.py
"""
import os
import re
import sys
from pathlib import Path

# Canonical source. Sync-PortalData.ps1 (Windows Task Scheduler, every
# 15 min) copies fusion-main-panel/src/ INTO fusion-bid-list/site/ —
# so the source of truth is the main-panel src. Patching site/ alone
# gets reverted on the next sync. Patching the source AND the mirror
# means the next deploy ships the right thing AND future syncs leave
# it alone.
SRC_PATH    = Path("c:/Users/AlexToler/Documents/Fusion Software/fusion-main-panel/src/index.html")
MIRROR_PATH = Path("c:/Users/AlexToler/Documents/Fusion Software/fusion-bid-list/site/index.html")
PATH = SRC_PATH   # back-compat for the rest of the script
src = SRC_PATH.read_text(encoding="utf-8")

# ── 0. Default page state: HIDE both login and stage. Inline boot
#       script below resolves the cached session synchronously and
#       shows whichever surface is correct — no flash either way.
#       Handles all three possible prior states of the HTML (original,
#       previously patched-shown, previously patched-hidden). ─────
import re as _re

# Login overlay → ALWAYS end up with style="display:none" inline
ov_re = _re.compile(r'<div id="loginOverlay" class="login-overlay[^"]*"[^>]*>')
src = ov_re.sub('<div id="loginOverlay" class="login-overlay" aria-hidden="true" style="display:none">', src, count=1)

# Main stage → ALWAYS end up with style="visibility:hidden" inline
st_re = _re.compile(r'<div class="stage[^"]*" id="mainStage"[^>]*>')
src = st_re.sub('<div class="stage" id="mainStage" style="visibility:hidden">', src, count=1)
# Inline pre-paint script: reads the cached master session from
# localStorage. Must run AFTER #mainStage is in the DOM, so we
# insert it just before the footer (which sits after the stage
# closes). If we put it earlier, getElementById("mainStage") is
# null and the script throws → falls into the "show login" branch
# even when the user is signed in. That was the actual cause of
# the persistent login-flash.
preboot_block = '''<script>
    (function preBoot(){
      try {
        var raw = localStorage.getItem("fusion_auth_user_full_v1");
        if (raw) {
          var u = JSON.parse(raw);
          if (u && Array.isArray(u.panels) && u.panels.length) {
            var stage = document.getElementById("mainStage");
            if (stage) {
              stage.style.visibility = "visible";
              stage.classList.remove("locked");
            }
            var allowed = {};
            for (var i = 0; i < u.panels.length; i++) allowed[u.panels[i]] = 1;
            var tiles = document.querySelectorAll(".panel-btn[data-panel]");
            for (var j = 0; j < tiles.length; j++) {
              tiles[j].style.display = allowed[tiles[j].getAttribute("data-panel")] ? "" : "none";
            }
            var so = document.getElementById("signOutBtn");
            if (so) so.style.display = "inline-block";
            return;
          }
        }
      } catch (e) {}
      // No cached session — show the login overlay.
      var ov = document.getElementById("loginOverlay");
      if (ov) { ov.style.display = ""; ov.classList.add("show"); ov.setAttribute("aria-hidden","false"); }
      var stg = document.getElementById("mainStage");
      if (stg) { stg.style.visibility = "visible"; stg.classList.add("locked"); }
    })();
  </script>
  '''

# 1. Remove any existing preBoot block (idempotent across moves).
import re as _re2
src = _re2.sub(
    r'<script>\s*\(function preBoot\(\).*?\}\)\(\);\s*</script>\s*',
    '',
    src,
    flags=_re2.DOTALL,
)

# 2. Insert just before <div class="footer"> (after the stage div has closed).
footer_marker = '<div class="footer">'
if footer_marker in src:
    src = src.replace(footer_marker, preboot_block + footer_marker, 1)
    print("  - moved pre-paint boot to AFTER stage div (post-DOM parse position)")
else:
    print("  - WARN: couldn't find footer marker for preBoot placement")

# ── 1. Replace the password login form with the Google button ──────────
old_form = (
    '<input id="loginEmail" class="login-input" type="email" placeholder="you@fusionelectric-inc.com" autocomplete="username" />\n'
    '      <input id="loginPass" class="login-input" type="password" placeholder="Password" autocomplete="current-password" />\n'
    '      <input id="loginPassConfirm" class="login-input" type="password" placeholder="Confirm password" autocomplete="new-password" style="display:none" />\n'
    '      <div id="loginError" class="login-error">Incorrect email or password.</div>\n'
    '      <div id="loginHint" class="login-error" style="color:#7dd3fc;display:none">First time on this device — pick a password (8+ chars).</div>\n'
    '      <button id="loginBtn" type="button" class="login-btn">Sign In</button>\n'
    '      <div style="margin-top:10px;font-size:11px;color:var(--muted);text-align:center">Restricted to authorized Fusion Electric staff.</div>'
)
new_form = '''<button id="googleSignIn" type="button"
        style="display:flex;align-items:center;justify-content:center;gap:10px;width:100%;background:#fff;color:#202124;border:1px solid #dadce0;border-radius:10px;padding:12px 14px;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit">
        <svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true">
          <path fill="#4285F4" d="M17.64 9.205c0-.639-.057-1.252-.164-1.841H9v3.481h4.844a4.14 4.14 0 0 1-1.796 2.716v2.259h2.908c1.702-1.567 2.684-3.875 2.684-6.615z"/>
          <path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"/>
          <path fill="#FBBC05" d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z"/>
          <path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"/>
        </svg>
        <span>Sign in with Google</span>
      </button>
      <div id="loginError" class="login-error" style="margin-top:14px;text-align:center"></div>
      <div style="margin-top:14px;font-size:11px;color:var(--muted);text-align:center">Restricted to authorized Fusion Electric staff.</div>'''
if old_form in src:
    src = src.replace(old_form, new_form)
    print("  - replaced password form with Google button")
elif "googleSignIn" in src:
    print("  - Google button already present")
else:
    sys.exit("FAIL: could not find login form to replace AND Google button not present")

# ── 2. Add data-panel attributes to existing tiles ─────────────────────
for path, panel in (("/bid-panel/", "bid"), ("/pm-panel/", "pm"), ("/field-panel/", "field")):
    pattern = re.compile(rf'<a class="panel-btn" href="{re.escape(path)}"(?! data-panel)')
    new = f'<a class="panel-btn" href="{path}" data-panel="{panel}"'
    src, n = pattern.subn(new, src)
    if n: print(f"  - tagged {path} as data-panel={panel}")

# ── 3. Add Ops Panel tile if missing ───────────────────────────────────
if 'data-panel="ops"' not in src:
    field_tile_end = (
        '        <div class="panel-meta">Field operations</div>\n'
        '        <span class="panel-arrow" aria-hidden="true">\n'
        '          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 5l7 7-7 7"/></svg>\n'
        '        </span>\n'
        '      </a>\n'
        '    </div>'
    )
    ops_tile = '''        <div class="panel-meta">Field operations</div>
        <span class="panel-arrow" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
        </span>
      </a>

      <a class="panel-btn" href="/ops-panel/" data-panel="ops" aria-label="Open Operations Panel">
        <span class="panel-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 3v18h18" />
            <path d="M7 14l4-4 4 4 5-7" />
            <circle cx="11" cy="10" r="1" />
            <circle cx="15" cy="14" r="1" />
          </svg>
        </span>
        <div class="panel-title">Ops Panel</div>
        <div class="panel-sub">Division health, estimator scorecard &amp; margin lens.</div>
        <div class="panel-meta">Admin / PM only</div>
        <span class="panel-arrow" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
        </span>
      </a>
    </div>'''
    if field_tile_end in src:
        src = src.replace(field_tile_end, ops_tile)
        print("  - inserted Ops Panel tile")
    else:
        print("  - WARN: couldn't find field-tile insertion point")
else:
    print("  - Ops Panel tile already present")

# ── 4. Replace the password auth IIFE with Google OAuth module ─────────
auth_iife_re = re.compile(
    r'    // --- Master login \(Fusion Electric domain\) ---.*?    \}\)\(\);\n',
    re.DOTALL,
)
new_auth = '''    // --- Google OAuth + per-user panel access ---
    // Sign-in lives here at the root. After Google auth, look up email
    // in app_users to confirm allowlist + read role. Tile visibility
    // is driven by PANEL_ACCESS below.
  </script>
  <script type="module">
    import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
    const SB_URL  = "https://dltuvsdwrujjsmiotaxy.supabase.co";
    const SB_ANON = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImRsdHV2c2R3cnVqanNtaW90YXh5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzcwNDU4NDIsImV4cCI6MjA5MjYyMTg0Mn0._lMgcZgERcgVULQ87BQFrNpZBssJeNtqN5LhhGsqE8Y";
    const sb = createClient(SB_URL, SB_ANON);

    const AUTH_SESSION_KEY = "fusion_auth_ok_v1";
    const AUTH_USER_KEY    = "fusion_auth_user_v1";
    const AUTH_FULL_KEY    = "fusion_auth_user_full_v1";

    const PANEL_ACCESS = {
      "alex@fusionelectric-inc.com":          ["bid", "pm", "field", "ops"],
      "jake@fusionelectric-inc.com":          ["bid", "pm", "field", "ops"],
      "gabriel.toler@fusionelectric-inc.com": ["bid", "pm", "field", "ops"],
      "johnpaulbuckley1170@gmail.com":        ["bid", "pm", "field", "ops"],
    };
    function roleDefault(role) {
      if (role === "admin")   return ["bid", "pm", "field", "ops"];
      if (role === "pm")      return ["bid", "pm", "ops"];
      if (role === "foreman") return ["field"];
      if (role === "field")   return ["field"];
      return [];
    }
    function panelsFor(email, role) {
      return PANEL_ACCESS[String(email || "").toLowerCase()] || roleDefault(role);
    }

    const overlay   = document.getElementById("loginOverlay");
    const stage     = document.getElementById("mainStage");
    const signOut   = document.getElementById("signOutBtn");
    const errEl     = document.getElementById("loginError");
    const googleBtn = document.getElementById("googleSignIn");

    function showError(msg) {
      if (msg) { errEl.textContent = msg; errEl.classList.add("show"); }
      else     { errEl.classList.remove("show"); }
    }
    function setSignedOut() {
      try { sessionStorage.removeItem(AUTH_SESSION_KEY); } catch (e) {}
      try { localStorage.removeItem(AUTH_FULL_KEY); } catch (e) {}
      overlay.style.display = "";
      overlay.classList.add("show");
      overlay.setAttribute("aria-hidden", "false");
      stage.style.visibility = "hidden";
      stage.classList.add("locked");
      signOut.style.display = "none";
    }
    function setSignedIn(user) {
      try {
        sessionStorage.setItem(AUTH_SESSION_KEY, "1");
        localStorage.setItem(AUTH_USER_KEY, user.email);
        localStorage.setItem(AUTH_FULL_KEY, JSON.stringify(user));
      } catch (e) {}
      overlay.style.display = "none";
      overlay.classList.remove("show");
      overlay.setAttribute("aria-hidden", "true");
      stage.style.visibility = "visible";
      stage.classList.remove("locked");
      signOut.style.display = "inline-block";
      const allowed = new Set(user.panels);
      document.querySelectorAll(".panel-btn[data-panel]").forEach(tile => {
        tile.style.display = allowed.has(tile.dataset.panel) ? "" : "none";
      });
    }
    async function lookupAppUser(email) {
      const url = `${SB_URL}/rest/v1/app_users?select=id,email,role,display_name,active,company_id,foreman_id,username&email=eq.${encodeURIComponent(email)}`;
      const r = await fetch(url, { headers: { apikey: SB_ANON, Authorization: `Bearer ${SB_ANON}` }});
      if (!r.ok) throw new Error(`app_users lookup failed (${r.status})`);
      const rows = await r.json();
      return rows[0] || null;
    }
    async function handleSession(session) {
      if (!session) { setSignedOut(); return; }
      const email = (session.user.email || "").toLowerCase();
      try {
        const row = await lookupAppUser(email);
        if (!row)         { showError(`${email} isn\\'t authorized yet. Contact your administrator.`); await sb.auth.signOut(); setSignedOut(); return; }
        if (!row.active)  { showError("Your account has been deactivated.");                  await sb.auth.signOut(); setSignedOut(); return; }
        const panels = panelsFor(email, row.role);
        if (!panels.length) { showError("You don\\'t have access to any panels yet.");        await sb.auth.signOut(); setSignedOut(); return; }
        setSignedIn({
          id: row.id, email, role: row.role,
          display_name: row.display_name,
          company_id: row.company_id,
          foreman_id: row.foreman_id,
          username: row.username,
          panels,
        });
      } catch (err) {
        showError(err.message || "Sign-in failed.");
        setSignedOut();
      }
    }
    googleBtn.addEventListener("click", async () => {
      googleBtn.disabled = true;
      const { error } = await sb.auth.signInWithOAuth({
        provider: "google",
        options: { redirectTo: window.location.origin + "/" },
      });
      if (error) { googleBtn.disabled = false; showError(error.message); }
    });
    signOut.addEventListener("click", async () => {
      await sb.auth.signOut();
      setSignedOut();
    });
    sb.auth.onAuthStateChange((_event, session) => handleSession(session));
    sb.auth.getSession().then(({ data }) => handleSession(data.session));
'''

m = auth_iife_re.search(src)
if m:
    src = src[:m.start()] + new_auth + src[m.end():]
    print("  - swapped password auth IIFE for Google OAuth module")
elif "createClient" in src and "signInWithOAuth" in src:
    print("  - Google OAuth module already present")
else:
    sys.exit("FAIL: could not find password auth IIFE to replace")

# ── 5. Idempotent message touch-ups (apply regardless of module state) ──
src = src.replace(
    "isn\\'t authorized. Ask Alex to add you.",
    "isn\\'t authorized yet. Contact your administrator.",
)
# Ops Panel tile label: was "Admin / PM only" before we tightened the
# role defaults; now ops access is admin-only by default (pm doesn't
# auto-inherit it). Per-email overrides in PANEL_ACCESS still apply.
src = src.replace(
    '<div class="panel-meta">Admin / PM only</div>',
    '<div class="panel-meta">Admin / OPS only</div>',
)
# Role-default tightening: pm no longer auto-gets ops. Explicit grant
# via PANEL_ACCESS map is the only way for a pm to see Ops Panel.
src = src.replace(
    'if (role === "pm")      return ["bid", "pm", "ops"];',
    'if (role === "pm")      return ["bid", "pm"];',
)

# ── 6a. Add init-phase guard so an early null session from
#       onAuthStateChange (fires before the SDK restores from
#       localStorage) doesn't briefly drop the user to the login UI.
old_listener = '    sb.auth.onAuthStateChange((_event, session) => handleSession(session));\n    sb.auth.getSession().then(({ data }) => handleSession(data.session));'
new_listener = '''    let _initialResolved = false;
    sb.auth.onAuthStateChange((_event, session) => {
      // Ignore null sessions until getSession() has definitively resolved.
      // Otherwise the SDK fires INITIAL_SESSION with null while it's still
      // restoring the cached JWT, and we briefly flash the login overlay.
      if (!session && !_initialResolved) return;
      handleSession(session);
    });
    sb.auth.getSession().then(({ data }) => {
      _initialResolved = true;
      handleSession(data.session);
    });'''
if old_listener in src:
    src = src.replace(old_listener, new_listener)
    print("  - added init-phase guard to auth state listener")
elif "_initialResolved" in src:
    print("  - init-phase guard already present")

# ── 6. Idempotent setSignedOut/setSignedIn rewrite (works whether the
#       module is fresh or already patched). Eliminates the login-flash
#       on bounce-back-from-child-panel by managing inline style.display
#       directly instead of relying on .show class vs inline display:none.
old_signed_out = (
    '    function setSignedOut() {\n'
    '      try { sessionStorage.removeItem(AUTH_SESSION_KEY); } catch (e) {}\n'
    '      try { localStorage.removeItem(AUTH_FULL_KEY); } catch (e) {}\n'
    '      overlay.classList.add("show");\n'
    '      stage.classList.add("locked");\n'
    '      signOut.style.display = "none";\n'
    '    }'
)
new_signed_out = (
    '    function setSignedOut() {\n'
    '      try { sessionStorage.removeItem(AUTH_SESSION_KEY); } catch (e) {}\n'
    '      try { localStorage.removeItem(AUTH_FULL_KEY); } catch (e) {}\n'
    '      overlay.style.display = "";\n'
    '      overlay.classList.add("show");\n'
    '      overlay.setAttribute("aria-hidden", "false");\n'
    '      stage.style.visibility = "hidden";\n'
    '      stage.classList.add("locked");\n'
    '      signOut.style.display = "none";\n'
    '    }'
)
if old_signed_out in src:
    src = src.replace(old_signed_out, new_signed_out)
    print("  - setSignedOut updated to manage inline style")

old_signed_in = (
    '      } catch (e) {}\n'
    '      overlay.classList.remove("show");\n'
    '      stage.classList.remove("locked");\n'
    '      signOut.style.display = "inline-block";'
)
new_signed_in = (
    '      } catch (e) {}\n'
    '      overlay.style.display = "none";\n'
    '      overlay.classList.remove("show");\n'
    '      overlay.setAttribute("aria-hidden", "true");\n'
    '      stage.style.visibility = "visible";\n'
    '      stage.classList.remove("locked");\n'
    '      signOut.style.display = "inline-block";'
)
if old_signed_in in src:
    src = src.replace(old_signed_in, new_signed_in)
    print("  - setSignedIn updated to manage inline style")

SRC_PATH.write_text(src, encoding="utf-8")
MIRROR_PATH.write_text(src, encoding="utf-8")
print(f"\nPatched {SRC_PATH.name} + mirror in site/. {len(src):,} bytes.")
print("Next: netlify deploy --prod --dir=site --functions=netlify/functions --skip-functions-cache")
