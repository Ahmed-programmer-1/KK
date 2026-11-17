"""mmgrab.py - AnimeZid / Megamax grabber v3 (ads blocked, no Megamax player, per-host strategies).

INPUT (any mix):
  * an AnimeZid episode  https://animezid.org/play.php?vid=...   (or watch.php?vid=...)   -> MegaMax first, then the page's other servers
  * an AnimeZid season/series page  https://animezid.org/series/boruto-ar/   (all episodes, use --from= --to= --seasons=)
  * a Megamax link  https://eg.megamax.cam/iframe/XXXX   (only the Megamax mirrors)

ORDER (always):
  1. MegaMax's inner hosts:  streamhg -> byse -> uqload        (best quality first inside each, any that exist)
  2. the page's own outer Uqload server
  3. MegaMax's lulustream     (own method: played in a browser tab and downloaded FROM INSIDE the player, like the old project)
  4. MegaMax's other hosts (earnvids, mp4upload, krakenfiles, ... and unknown new ones)
  5. the page's other outer servers (DoodStream, VidTube, ...)
  a failed step just moves to the next one; the first success is downloaded at once (links are signed and expire).

FILE NAMES: the episode number only -> 1.mp4, 2.mp4, 3.mp4 ... (own folder per season only when several seasons are collected together).
Anti-adblock walls: a player that refuses to SHOW the video still gets its m3u8 caught (network + JS state + page source); if a wall shows and
nothing was caught, that host is retried once without blocking.

Usage:  py mmgrab.py "<link>" [more links] [--file=links.txt] [--out=downloads] [--name=Boruto-220]
Options:
  --quality=best|1080|720|480   highest quality to try        --only=streamhg,byse   try only these hosts
  --no-lulu                     skip lulustream               --no-outer             ignore the AnimeZid page's own servers
  --from=5 --to=20 --seasons=1  episode range for a series    --grab-only            find + verify links, no download
  --wait=75 --idle=20           max seconds per host / give up when nothing loads for this long (slow net: --wait=120 --idle=40)
  --min-minutes=10              reject shorter media (decoys)  --workers=16           parallel segments (python method)
  --headless                    hide the browser (try visible first)
Needs: pip install playwright requests && playwright install chromium   (+ ffmpeg on PATH)
"""
import base64, json, os, re, shutil, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


def opt(name, default, cast=str):
    for a in sys.argv:
        if a.startswith(f"--{name}="):
            try:
                return cast(a.split("=", 1)[1])
            except ValueError:
                pass
    return default


MAXWAIT = opt("wait", 75.0, float)
IDLE = opt("idle", 20.0, float)
MIN_MIN = opt("min-minutes", 10.0, float)
WORKERS = opt("workers", 16, int)
OUT = opt("out", "downloads")
QUALITY = opt("quality", "best")
ONLY = [x for x in opt("only", "").lower().split(",") if x]
NO_LULU = "--no-lulu" in sys.argv
NO_OUTER = "--no-outer" in sys.argv
GRAB_ONLY = "--grab-only" in sys.argv
HEADLESS = "--headless" in sys.argv
EP_FROM, EP_TO = opt("from", None, float), opt("to", None, float)
SEASONS = [int(x) for x in opt("seasons", "").split(",") if x.strip().isdigit()]
MEGA_REFERER = "https://eg.megamax.cam/"
AZ_REFERER = "https://animezid.org/"

# ---- host preference ----
PREFERRED = [("streamhg", "streamwish", "hgcloud"), ("byse",), ("uqload",)]
OTHERS = ["earnvids", "filelions", "vidhide", "mp4upload", "krakenfiles", "voe", "filemoon", "streamtape", "doodstream", "dood", "mixdrop"]
LULU = ("lulustream", "lulu", "luluvid")

# ---- ad blocking ----
AD_DOMAINS = ("ser-46-host.store", "agl006.host", "asiafilm.org", "directyp.org", "xbeat.space", "nresystems.com", "predictivadnetwork.com",
              "histats.com", "googlesyndication.com", "googletagmanager.com", "google-analytics.com", "doubleclick.net", "adsterra", "popads",
              "monetag", "propellerads", "exoclick", "juicyads", "popcash", "clickadu", "hilltopads", "adcash", "trafficjunky", "onclick",
              "bidgear", "richads", "pushnami", "a-ads.com", "admaven", "yllix", "tsyndicate", "profitableratecpm", "effectivegatecpm",
              "cpmstar", "adskeeper", "mgid.com", "traffmovie", "burnetsasgmt", "clarity.ms", "cloudflareinsights.com", "adangle",
              "1xbet", "betwinner", "melbet", "linebet", "mostbet", "creative-cdn", "adexchangeclear", "adnxs", "criteo", "taboola", "outbrain")
AD_PATH = ("/content/stream/agl/", "/vast", "/ads/", "adserve", "popunder", "/banner")
NOT_VIDEO = ("preview", "thumb", "sprite", "trailer", "/ads/", "banner", "1xbet", "/content/stream/agl/")
MEDIA_RX = re.compile(r"\.(m3u8|mp4)(\?|#|$)|/master\.txt|urlset/master|/hls\d*/[^?]*\.txt", re.I)
ADBLOCK = [True]          # the AnimeZid page itself is retried once WITHOUT blocking if blocking leaves it without a player

INIT_JS = """(() => {
  try { window.open = () => ({closed:false, focus(){}, blur(){}, close(){}, postMessage(){}, document:{write(){},close(){}}, location:{href:''}}); } catch(e){}
  try { Object.defineProperty(navigator,'webdriver',{get:()=>undefined}); } catch(e){}
  document.addEventListener('click', e => { const a = e.target && e.target.closest && e.target.closest('a[target=_blank]'); if (a) e.preventDefault(); }, true);
})();"""
JS_PLAY = """() => {
  try { document.querySelectorAll('video').forEach(v => { v.muted = true; const p = v.play(); if (p && p.catch) p.catch(()=>{}); }); } catch(e){}
  try { document.querySelectorAll('.jw-icon-display,.jw-display-icon-container,.vjs-big-play-button,.plyr__control--overlaid,#play,[class*="play-btn"],[class*="play_btn"],[aria-label="Play"],.fp-play,.play-button').forEach(b => b.click()); } catch(e){}
  try { if (window.jwplayer) jwplayer().play(); } catch(e){}
}"""
JS_PAUSE = "() => { document.querySelectorAll('video').forEach(v => { try { v.muted = true; v.pause(); } catch (e) {} }); }"
JS_VSTATE = """() => { let loading=false, playing=false, has=false;
  document.querySelectorAll('video').forEach(v => { has=true; if (!v.paused && v.readyState>=2) playing=true;
    if (!v.ended && (v.networkState===2 || (v.readyState<3 && (v.currentSrc||v.src)))) loading=true; });
  return {has, playing, loading}; }"""
JS_INERTIA = """async (u) => {
  const ck = decodeURIComponent((document.cookie.match(/XSRF-TOKEN=([^;]+)/) || [])[1] || '');
  const pg = JSON.parse(document.querySelector('script[data-page]').textContent);
  const r = await fetch(u, {headers: {'Accept': 'text/html, application/xhtml+xml', 'X-Inertia': 'true', 'X-Inertia-Version': pg.version,
     'X-Inertia-Partial-Component': pg.component, 'X-Inertia-Partial-Data': 'streams', 'X-Inertia-Except-Once-Props': 'video,config,ads',
     'X-Requested-With': 'XMLHttpRequest', 'X-XSRF-TOKEN': ck}, credentials: 'include'});
  return {status: r.status, text: await r.text(), name: (pg.props && pg.props.video && pg.props.video.name) || ''};
}"""
# server buttons of the AnimeZid watch page (same finder the old project used)
SERVER_RX = (r"megamax|vidtube|yourupload|videa|sendvid|streamlare|mega\b|sibnet|vidmoly|4meplayer|michi|google|\bok\b|kraken|streamhg|turbovid|vidhls|leech|earnvids|streamruby|"
             r"mixdrop|dood|uqload|voe|filemoon|streamwish|vidhide|lulustream|streamtape|mp4upload|okru|ok\.ru|vidoza|upstream|hgcloud|vkvideo")
JS_SERVERS = r"""(a) => {
  const re = new RegExp(a.rx, 'i'), bad = /جارٍ|loading|اختر|السيرفر الحالي|select|choose|تحميل|download|share|مشاركة|إبلاغ|report|login|تسجيل/i;
  const qual = /^[\w.\-]+\s+(?:FHD|HD|SD|4K|1080p?|720p?|480p?|360p?)$/i;
  document.querySelectorAll('[data-hx]').forEach(e => { delete e.dataset.hx; });
  const n = s => (s || '').trim().replace(/\s+/g, ' ');
  const first = el => n((el.innerText || '').split('\n')[0]);
  const dl = [...document.querySelectorAll('h1,h2,h3,h4,h5')].find(h => /سيرفرات التحميل|روابط التحميل/.test(h.textContent || ''));
  const clickable = el => !!el.closest('li, a, button, [role="tab"], [onclick], [tabindex]') || getComputedStyle(el).cursor === 'pointer';
  const rows = new Map();
  const add = (el, name) => {
    const row = el.closest('li, a, button, [role="tab"]') || el, r = row.getBoundingClientRect();
    if (!name || name.length > 30 || bad.test(name) || row.closest('nav, header, footer') || !(r.width > 0 && r.height > 0)) return;
    if (dl && (dl.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING)) return;
    const key = name.toLowerCase(), c = clickable(el), old = rows.get(key);
    if (!old || (c && !old.c)) rows.set(key, {row, name, c});
  };
  document.querySelectorAll('[data-server],[data-embed],[class*="server" i] li,[class*="server" i] a,[class*="server" i] button,[id*="server" i] li,[id*="server" i] a,[id*="server" i] button')
    .forEach(el => add(el, first(el) || n(el.getAttribute('data-name') || el.title)));
  document.querySelectorAll('body *').forEach(el => {
    const t = first(el);
    if (!t || t.length > 30 || [...el.children].some(c => first(c) === t)) return;
    if ((re.test(t) && t.length <= 25) || qual.test(t)) add(el, t);
  });
  return [...rows.values()].map((r, i) => { r.row.dataset.hx = String(i); return {idx: i, name: r.name}; });
}"""
JS_IFRAMES = r"""() => [...document.querySelectorAll('iframe')].map(f => f.src || f.getAttribute('data-src') || '').filter(Boolean)"""
JS_ANCHORS = r"""() => [...document.querySelectorAll('a[href]')].map(a => ({href: a.href, text: (a.innerText || '').replace(/\s+/g, ' ').trim(), title: (a.title || '').trim()}))"""

# in-player HLS downloader (from the old project): runs INSIDE the player frame, same origin / TLS / cookies as the video itself
JS_HLS_DL = r"""
async (cfg) => {
  const P = window.__mmp = {state: "start", done: 0, total: 0, bytes: 0, err: null, limit: cfg.conc, fails: 0, dur: 0};
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  try {
    const abs = (u, b) => new URL(u, b).href;
    const getText = async (u) => { const r = await fetch(u); if (!r.ok) throw new Error("HTTP " + r.status + " " + u.slice(0, 100)); return r.text(); };
    const getBuf = async (u) => { const r = await fetch(u); if (!r.ok) throw new Error("HTTP " + r.status + " " + u.slice(0, 100)); return r.arrayBuffer(); };
    const retry = async (fn) => { let last; for (let t = 0; t < cfg.retries; t++) { try { return await fn(); } catch (e) { last = e; await sleep(Math.min(cfg.backoff * 1000 * 2 ** t, 6000)); } } throw last; };
    let base = cfg.url, text = await retry(() => getText(base));
    if (text.includes("#EXT-X-STREAM-INF")) {
      const L = text.split(/\r?\n/); let best = null, bw = -1;
      for (let i = 0; i < L.length; i++) if (L[i].startsWith("#EXT-X-STREAM-INF")) {
        const m = /BANDWIDTH=(\d+)/.exec(L[i]); const b = m ? +m[1] : 0;
        let j = i + 1; while (j < L.length && (L[j].trim() === "" || L[j].startsWith("#"))) j++;
        if (j < L.length && b > bw) { bw = b; best = L[j].trim(); }
      }
      if (!best) throw new Error("no variant in master playlist");
      base = abs(best, base); text = await retry(() => getText(base));
    }
    P.dur = [...text.matchAll(/#EXTINF:([\d.]+)/g)].reduce((a, m) => a + +m[1], 0);
    let key = null, map = null, seq = 0; const segs = [];
    for (const raw of text.split(/\r?\n/)) {
      const s = raw.trim();
      if (s.startsWith("#EXT-X-MEDIA-SEQUENCE:")) seq = parseInt(s.split(":")[1]) || 0;
      else if (s.startsWith("#EXT-X-KEY:")) {
        const meth = /METHOD=([^,]+)/.exec(s)[1];
        if (meth === "NONE") key = null;
        else if (meth !== "AES-128") throw new Error("unsupported encryption " + meth);
        else { const u = /URI="([^"]+)"/.exec(s)[1], iv = /IV=0x([0-9a-fA-F]+)/.exec(s); key = {uri: abs(u, base), iv: iv ? iv[1] : null}; }
      } else if (s.startsWith("#EXT-X-MAP:")) map = abs(/URI="([^"]+)"/.exec(s)[1], base);
      else if (s && !s.startsWith("#")) segs.push({u: abs(s, base), key, seq: seq++});
    }
    if (!segs.length) throw new Error("empty media playlist");
    P.ready = true;
    if (cfg.minsec && P.dur < cfg.minsec) throw new Error("too short: " + Math.round(P.dur / 60) + " min");
    const jobs = []; if (map) jobs.push([-1, map, null, 0]);
    segs.forEach((s, i) => jobs.push([i, s.u, s.key, s.seq]));
    P.total = jobs.length;
    const have = new Set(cfg.have || []);
    const todo = jobs.filter(j => !have.has(j[0])); P.done = jobs.length - todo.length;
    const keys = {};
    const getKey = async (k) => keys[k.uri] || (keys[k.uri] = crypto.subtle.importKey("raw", await getBuf(k.uri), "AES-CBC", false, ["decrypt"]));
    const ivOf = (k, n) => { if (k.iv) { const h = k.iv.padStart(32, "0"); return Uint8Array.from(h.match(/../g).map(x => parseInt(x, 16))); }
      const a = new Uint8Array(16); new DataView(a.buffer).setUint32(12, n); return a; };
    const b64 = (buf) => new Promise(res => { const f = new FileReader(); f.onload = () => res(f.result.split(",")[1]); f.readAsDataURL(new Blob([buf])); });
    let streak = 0;
    const work = async (idx, u, k, n) => {
      let last = null;
      for (let t = 0; t < cfg.retries; t++) {
        try {
          let buf = await getBuf(u);
          if (k) buf = await crypto.subtle.decrypt({name: "AES-CBC", iv: ivOf(k, n)}, await getKey(k), buf);
          if (!(await window.__mmsave(idx, await b64(buf)))) throw new Error("save failed");
          P.bytes += buf.byteLength; P.done++;
          if (++streak >= 15 && P.limit < cfg.conc) { P.limit++; streak = 0; }
          return;
        } catch (e) { last = e; P.fails++; streak = 0; P.limit = Math.max(1, P.limit - 1); await sleep(Math.min(cfg.backoff * 1000 * 2 ** t, 6000)); }
      }
      throw last;
    };
    let next = 0;
    await Promise.all(Array.from({length: cfg.conc}, async (_, w) => {
      while (next < todo.length && !P.err) {
        if (w >= P.limit) { await sleep(300); continue; }
        const j = todo[next++];
        try { await work(...j); } catch (e) { P.err = String((e && e.message) || e); }
      }
    }));
    if (P.err) throw new Error(P.err);
    P.state = "done";
  } catch (e) { P.err = String((e && e.message) || e); P.state = "error"; }
}
"""


def log(*a):
    print(*a, flush=True)


# --- the player may refuse to SHOW the video (anti-adblock wall) and still produce / contain the m3u8: look in the page too ---
PACKED_RE = re.compile(r"}\('(.*)',(\d+),(\d+),'(.*?)'\.split\('\|'\)", re.S)
MEDIA_TEXT_RE = re.compile(r'https?:?(?:\\?/){2}(?:[^\s"\'<>\\]|\\/)+?\.(?:m3u8|mp4)(?:[^\s"\'<>\\]|\\/)*', re.I)   # also JS-escaped urls (https:\/\/host\/a.m3u8)
WALL_RX = re.compile(r"turn off .{0,20}ad ?block|disable .{0,20}ad ?block|ad ?block.{0,30}(detected|enabled)|أوقف.{0,15}(مانع|حاجب)|عطل.{0,15}(مانع|حاجب)", re.I)
JS_SCAN = """() => {
  const out = [];
  try { performance.getEntriesByType('resource').forEach(e => out.push(e.name)); } catch (e) {}
  try { document.querySelectorAll('video,source').forEach(v => { if (v.currentSrc) out.push(v.currentSrc); if (v.src) out.push(v.src); }); } catch (e) {}
  try { if (window.jwplayer) { const p = jwplayer().getPlaylist() || []; p.forEach(i => { (i.sources || []).forEach(s => out.push(s.file)); if (i.file) out.push(i.file); }); } } catch (e) {}
  try { if (window.videojs) Object.values(videojs.getPlayers()).forEach(p => { if (p && p.currentSrc) out.push(p.currentSrc()); }); } catch (e) {}
  return out.filter(x => typeof x === 'string' && x);
}"""
JS_WALL = "() => (document.body && document.body.innerText || '').slice(0, 600)"


def _base(n, base):
    d = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return d[n] if n < base else _base(n // base, base) + d[n % base]


def unpack(text):
    out = []
    for m in PACKED_RE.finditer(text):
        payload, a, c, words = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|")
        table = {}
        for i in range(c):
            table[_base(i, a)] = words[i] if i < len(words) and words[i] else _base(i, a)
        out.append(re.sub(r"\b\w+\b", lambda x: table.get(x.group(0), x.group(0)), payload.replace("\\'", "'")))
    return "\n".join(out)


def find_in_text(text, page_url):
    import html as _h
    text = text + "\n" + unpack(text)
    found = []
    for m in MEDIA_TEXT_RE.finditer(text):
        u = _h.unescape(m.group(0).replace("\\/", "/")).rstrip("\\")
        u = "https:" + u if u.startswith("//") else u
        if u not in found:
            found.append(u)
    for m in re.finditer(r'["\'](/[^"\'\s]+\.(?:m3u8|mp4)[^"\'\s]*)["\']', text):
        u = urljoin(page_url, m.group(1))
        if u not in found:
            found.append(u)
    return found


def safe_name(s):
    return re.sub(r"[^\w\-.]+", "_", s, flags=re.UNICODE).strip("_.") or "video"


def is_ad(url):
    u = url.lower()
    host = urlparse(u).hostname or ""
    return any(d in host for d in AD_DOMAINS) or any(p in u for p in AD_PATH)


def is_media(url):
    if not url or url.startswith(("blob:", "data:")) or is_ad(url):
        return False
    u = url.lower()
    return bool(MEDIA_RX.search(u)) and not any(w in u for w in NOT_VIDEO)


def rank(u):
    p = u.lower().split("?")[0]
    if p.endswith(".m3u8"):
        return 0 if "master" in p else 1
    return 3 if p.endswith(".mp4") else 2


# ================================================================= ordering (pure logic)
def height_of(q):
    m = re.search(r"(\d{3,4})x(\d{3,4})", q.get("resolution") or "")
    if m:
        return int(m.group(2))
    m = re.search(r"(\d{3,4})p", q.get("label") or "")
    return int(m.group(1)) if m else 0


def host_tier(driver):
    """MegaMax inner host -> (group, index) | None.   0 preferred, 1 lulustream, 2 others"""
    d = (driver or "").lower()
    if ONLY and not any(o in d for o in ONLY):
        return None
    for i, keys in enumerate(PREFERRED):
        if any(k in d for k in keys):
            return (0, i)
    if any(k in d for k in LULU):
        return None if NO_LULU else (1, 0)
    for i, k in enumerate(OTHERS):
        if k in d:
            return (2, i)
    return (2, 99)


def build_candidates(streams):
    maxh = int(QUALITY) if str(QUALITY).isdigit() else 99999
    out, seen = [], set()
    for q in streams.get("data", []):
        h = height_of(q)
        if h and h > maxh:
            continue
        for m in q.get("mirrors", []):
            t = host_tier(m.get("driver"))
            link = m.get("link") or ""
            if t is None or not link or link in seen:
                continue
            seen.add(link)
            out.append({"type": "mega", "group": t[0], "hidx": t[1], "height": h, "label": q.get("label"), "driver": m["driver"],
                        "embed": "https:" + link if link.startswith("//") else link, "method": "browser" if t[0] == 1 else "python"})
    out.sort(key=lambda c: (c["group"], -c["height"], c["hidx"]))
    return out


def build_plan(mega, outer_names):
    """mega: build_candidates() result;  outer_names: AnimeZid page server names (MegaMax already removed)"""
    outer = []
    for i, n in enumerate(outer_names):
        if ONLY and not any(o in n.lower() for o in ONLY):
            continue
        outer.append({"type": "outer", "driver": n, "label": "page", "height": 0, "method": "python", "embed": None,
                      "pref": "uqload" in n.lower(), "i": i})
    return ([c for c in mega if c["group"] == 0] + [o for o in outer if o["pref"]] + [c for c in mega if c["group"] == 1]
            + [c for c in mega if c["group"] == 2] + [o for o in outer if not o["pref"]])


# ================================================================= browser plumbing
def route(r):
    try:
        rq = r.request
        if is_ad(rq.url):
            try:
                if rq.is_navigation_request() and rq.frame.parent_frame is None:
                    return r.fulfill(status=204, body="")      # an ad tried to take over the tab
            except Exception:
                pass
            if ADBLOCK[0]:
                return r.abort()
        r.continue_()
    except Exception:
        pass


def _close_popup(pg):
    try:
        if pg.opener() is not None:
            pg.close()
    except Exception:
        pass


JOBS = {}      # page -> download job (the player frame hands segments to python through __mmsave)


def _save_binding(source, idx, b64):
    try:
        job = JOBS.get(source["page"])
        if not job:
            return False
        data = base64.b64decode(b64)
        with open(os.path.join(job["dir"], f"{int(idx) + 1:07d}.part"), "wb") as f:
            f.write(data)
        job["bytes"] += len(data)
        return True
    except Exception:
        return False


def new_context(p):
    # web security is relaxed on purpose: the in-player downloader must read cross-origin segments and real HTTP codes (FetchV style)
    b = p.chromium.launch(headless=HEADLESS, args=["--disable-blink-features=AutomationControlled", "--autoplay-policy=no-user-gesture-required",
                                                   "--disable-web-security", "--disable-site-isolation-trials"])
    ctx = b.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
    ctx.add_init_script(INIT_JS)
    ctx.route("**/*", route)
    ctx.on("page", _close_popup)
    ctx.expose_binding("__mmsave", _save_binding)
    return b, ctx


class Catcher:
    """records the media requests of one page (all its frames) and whether anything is still loading"""

    def __init__(self, page):
        self.seen, self.inflight, self.last, self.t0 = {}, {}, time.time(), time.time()
        page.on("request", self.on_req)
        page.on("requestfinished", self.on_done)
        page.on("requestfailed", self.on_done)
        page.on("response", self.on_resp)

    def on_req(self, rq):
        try:
            if is_ad(rq.url):
                return
            self.last = time.time()
            if rq.resource_type in ("xhr", "fetch", "document", "script", "media") or is_media(rq.url):
                self.inflight[rq.url] = time.time()
            if is_media(rq.url) and rq.url not in self.seen:
                self.seen[rq.url] = {"headers": rq.all_headers(), "status": None, "t": round(time.time() - self.t0, 1),
                                     "frame": rq.frame, "frame_url": rq.frame.url}
        except Exception:
            pass

    def on_done(self, rq):
        self.inflight.pop(rq.url, None)
        self.last = time.time()

    def on_resp(self, rs):
        try:
            if rs.url in self.seen:
                self.seen[rs.url]["status"] = rs.status
        except Exception:
            pass


def click_player(page):
    best, area = None, 0
    for fr in page.frames[1:]:
        try:
            if is_ad(fr.url) or not fr.url.startswith("http"):
                continue
            box = fr.frame_element().bounding_box()
            if box and box["width"] * box["height"] > area:
                best, area = box, box["width"] * box["height"]
        except Exception:
            continue
    try:
        if best:
            page.mouse.click(best["x"] + best["width"] / 2, best["y"] + best["height"] / 2)
        else:
            vs = page.viewport_size or {"width": 1280, "height": 720}
            page.mouse.click(vs["width"] / 2, vs["height"] / 2)
    except Exception:
        pass


def wait_media(page, cat):
    """adaptive wait: goes on while the host is still loading (slow servers), stops at once when a valid stream shows up"""
    why, i, t0 = f"max wait {MAXWAIT:.0f}s", 0, time.time()
    while time.time() - t0 < MAXWAIT:
        page.wait_for_timeout(1000)
        i += 1
        el = time.time() - t0
        ok = {u: d for u, d in cat.seen.items() if d["status"] in (200, 206)}
        if any(".m3u8" in u or "master" in u for u in ok) and el > 3:
            return "m3u8 caught"
        if ok and el > 8:
            return "media caught"
        if not cat.seen and i % 4 == 0:
            scan_frames(page, cat)
        if any(d.get("static") for d in cat.seen.values()) and not ok and el > 3:
            return "found in page source"
        st = {"has": False, "playing": False, "loading": False}
        for fr in page.frames:
            try:
                r = fr.evaluate(JS_VSTATE)
                for k in st:
                    st[k] = st[k] or r[k]
            except Exception:
                pass
        for u, t in list(cat.inflight.items()):
            if time.time() - t > 40:
                cat.inflight.pop(u, None)
        if not (cat.inflight or st["loading"] or time.time() - cat.last < IDLE) and el > IDLE:
            return f"idle {IDLE:.0f}s (nothing loading)"
        if not st["playing"]:
            for fr in page.frames:
                try:
                    fr.evaluate(JS_PLAY)
                except Exception:
                    pass
            if i in (2, 5, 9, 14, 20, 28, 38, 50, 62) and not cat.seen:
                click_player(page)
    return why


def close(page):
    try:
        page.close()
    except Exception:
        pass


def scan_frames(page, cat):
    """links the player holds in its JS state / page source even if it never requested them (e.g. blocked by its anti-adblock wall)"""
    n = 0
    for fr in page.frames:
        try:
            if not fr.url.startswith("http") or is_ad(fr.url):
                continue
            cands = [urljoin(fr.url, u) for u in (fr.evaluate(JS_SCAN) or [])] + find_in_text(fr.content(), fr.url)
        except Exception:
            continue
        for u in cands:
            if is_media(u) and u not in cat.seen:
                cat.seen[u] = {"headers": {}, "status": None, "t": round(time.time() - cat.t0, 1), "frame": fr, "frame_url": fr.url, "static": True}
                n += 1
    return n


def wall_seen(page):
    for fr in page.frames:
        try:
            if WALL_RX.search(fr.evaluate(JS_WALL) or ""):
                return True
        except Exception:
            continue
    return False


# ================================================================= MegaMax mirrors
def get_mirrors(ctx, url):
    last = None
    for attempt in range(3):
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(30):
                if "just a moment" not in (page.title() or "").lower():
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(1500)
            res = page.evaluate(JS_INERTIA, page.url)
            if res["status"] == 200:
                d = json.loads(res["text"])["props"]["streams"]
                if d.get("data"):
                    return d, res.get("name") or ""
            last = f"status {res['status']}"
        except Exception as e:
            last = str(e).splitlines()[0]
        finally:
            close(page)
        time.sleep(2)
    raise RuntimeError(f"could not read the mirror list ({last})")


# ================================================================= AnimeZid watch page
def press_overlay(page):
    for pat in (r"اضغط هنا لتشغيل", r"تشغيل الحلقة", r"^\s*(?:play|شغل|شاهد الآن|مشاهدة الآن)\s*$"):
        try:
            loc = page.get_by_text(re.compile(pat, re.I)).first
            if loc.count():
                try:
                    loc.click(timeout=2500)
                except Exception:
                    loc.dispatch_event("click")
                return True
        except Exception:
            continue
    return False


def list_servers(page, seconds=10):
    end = time.time() + seconds
    while True:
        try:
            found = page.evaluate(JS_SERVERS, {"rx": SERVER_RX})
        except Exception:
            found = []
        uniq = {}
        for r in found:
            uniq.setdefault(r["name"].lower(), r)
        items = [(page.locator(f'[data-hx="{r["idx"]}"]').first, r["name"]) for r in uniq.values()]
        if items or time.time() >= end:
            return items
        page.wait_for_timeout(700)


def play_url_of(url):
    u = urlparse(url)
    vid = (parse_qs(u.query).get("vid") or [""])[0]
    return f"{u.scheme}://{u.netloc}/play.php?vid={vid}"


def open_play(ctx, play_url):
    page = ctx.new_page()
    try:
        page.goto(play_url, wait_until="domcontentloaded", timeout=60000, referer=AZ_REFERER)
    except Exception as e:
        log("      watch page did not load:", str(e).splitlines()[0])
    page.wait_for_timeout(1500)
    items = list_servers(page, 4)
    if not items:
        if press_overlay(page):
            page.wait_for_timeout(1500)
        items = list_servers(page, 10)
    return page, items


def megamax_in(page):
    for fr in page.frames:
        if re.search(r"(megamax|share4max)[^/]*/iframe/[A-Za-z0-9]{8,}", fr.url) or re.search(r"/iframe/[A-Za-z0-9]{8,}/?$", urlparse(fr.url).path):
            if "animezid" not in urlparse(fr.url).netloc:
                return fr.url
    for src in (page.evaluate(JS_IFRAMES) or []):
        if re.search(r"(megamax|share4max)[^/]*/iframe/[A-Za-z0-9]{8,}", src):
            return "https:" + src if src.startswith("//") else src
    return None


def find_megamax(ctx, play_url):
    """-> (megamax iframe url | None, [outer server names in page order, MegaMax removed])"""
    page, items = open_play(ctx, play_url)
    names = [n for _, n in items]
    mega_url = None
    reqs = []
    page.on("request", lambda rq: reqs.append(rq.url) if re.search(r"(megamax|share4max)[^/]*/iframe/", rq.url) else None)
    try:
        mm = next((loc for loc, n in items if "mega" in n.lower()), None)
        if mm is not None:
            mega_url = megamax_in(page)          # the page may already have auto-selected it
            if not mega_url:
                try:
                    mm.click(timeout=5000)
                except Exception:
                    mm.dispatch_event("click")
            for _ in range(24):
                mega_url = mega_url or megamax_in(page) or next((u for u in reqs if "/iframe/" in u), None)
                if mega_url:
                    break
                page.wait_for_timeout(500)
    finally:
        title = ""
        try:
            title = page.title()
        except Exception:
            pass
        close(page)
    outer = [n for n in names if "mega" not in n.lower()]
    return mega_url, outer, title


EP_RX = re.compile(r"(?:الحلقة|حلقة|الحلقه|حلقه|episode|ep)[\s\-]*(\d+(?:\.\d+)?)", re.I)


def num_str(x):
    x = float(x)
    return str(int(x)) if x == int(x) else str(x)


def ep_number(*texts):
    """episode number found in a page title / file name (Arabic or English), as '1', '2' ... or None"""
    for t in texts:
        for rx in (EP_RX, re.compile(r"S\d+E(\d+)", re.I), re.compile(r"\bE(\d{1,4})\b")):
            m = rx.search(t or "")
            if m:
                return num_str(m.group(1))
    return None


def collect_episodes(ctx, url):
    page = ctx.new_page()
    eps, ep_rx = {}, EP_RX
    u = urlparse(url)
    slug = (re.match(r"/series/([^/]+)", unquote(u.path)) or [None, "series"])[1]

    def anchors(link):
        try:
            page.goto(link, wait_until="domcontentloaded", timeout=60000, referer=AZ_REFERER)
            page.wait_for_timeout(1200)
            return page.evaluate(JS_ANCHORS) or []
        except Exception as e:
            log("   could not load", link, str(e).splitlines()[0])
            return []
    try:
        first = anchors(url)
        base = f"{u.scheme}://{u.netloc}"
        if re.fullmatch(r"/series/[^/]+/season/\d+/?", unquote(u.path)):
            seasons = [(int(re.search(r"season/(\d+)", u.path).group(1)), url.split("?")[0])]
        else:
            ss = {}
            for a in first:
                m = re.fullmatch(r"/series/[^/]+/season/(\d+)/?", unquote(urlparse(a["href"].split("?")[0]).path))
                if m:
                    ss[int(m.group(1))] = a["href"].split("?")[0].split("#")[0]
            seasons = sorted(ss.items()) or [(1, url.split("?")[0])]
        if SEASONS:
            seasons = [s for s in seasons if s[0] in SEASONS]
        for sn, surl in seasons:
            for pg in range(1, 60):
                rows = anchors(surl if pg == 1 else f"{surl}?page={pg}")
                new = 0
                for a in rows:
                    p = urlparse(a["href"])
                    vid = (parse_qs(p.query).get("vid") or [""])[0]
                    if "watch.php" not in p.path or not vid or re.search(r"التالية|السابقة", a["text"]) or vid in eps:
                        continue
                    m = ep_rx.search(a["text"]) or ep_rx.search(a["title"])
                    if m:
                        eps[vid] = {"vid": vid, "season": sn, "num": float(m.group(1)), "href": f"{base}/play.php?vid={vid}"}
                        new += 1
                log(f"   season {sn} page {pg}: {new} new episode(s)")
                if not new:
                    break
    finally:
        close(page)
    out = sorted(eps.values(), key=lambda e: (e["season"], e["num"]))
    out = [e for e in out if (EP_FROM is None or e["num"] >= EP_FROM) and (EP_TO is None or e["num"] <= EP_TO)]
    multi = len({e["season"] for e in out}) > 1
    for e in out:
        e["label"] = num_str(e["num"])                      # 1, 2, 3 ... (12.5 for a special) - no zero padding, no words
        e["sub"] = f"S{e['season']}" if multi else ""       # only when several seasons are collected together: one folder per season
    return out


# ================================================================= verification + python download
def get(url, h, **kw):
    return requests.get(url, headers=h, timeout=kw.pop("timeout", 20), **kw)


def best_variant(master_url, txt):
    best, bw = None, -1
    lines = txt.splitlines()
    for i, l in enumerate(lines):
        if l.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", l)
            b = int(m.group(1)) if m else 0
            nxt = next((x.strip() for x in lines[i + 1:] if x.strip() and not x.startswith("#")), None)
            if nxt and b > bw:
                best, bw = urljoin(master_url, nxt), b
    return best


def header_variants(browser_headers, embed, frame_url=None):
    skip = {"host", "content-length", "accept-encoding", "connection", "range", "if-range"}
    v = []
    if browser_headers:
        v.append(("browser-exact", {k: x for k, x in browser_headers.items() if k.lower() not in skip}))
    for name, ref in (("frame-referer", frame_url), ("embed-referer", embed), ("megamax-referer", MEGA_REFERER), ("animezid-referer", AZ_REFERER)):
        if ref and ref.startswith("http"):
            o = urlparse(ref)
            v.append((name, {"User-Agent": UA, "Referer": ref, "Origin": f"{o.scheme}://{o.netloc}"}))
    v.append(("ua-only", {"User-Agent": UA}))
    return v


def verify(url, browser_headers, embed, frame_url=None):
    why = []
    for name, h in header_variants(browser_headers, embed, frame_url):
        try:
            if re.search(r"\.m3u8|master|/hls", url.lower()):
                r = get(url, h)
                if r.status_code != 200 or not r.text.lstrip().startswith("#EXTM3U"):
                    why.append(f"{name}: {r.status_code}")
                    continue
                txt, purl = r.text, url
                if "#EXT-X-STREAM-INF" in txt:
                    bv = best_variant(url, txt)
                    r2 = get(bv, h)
                    if r2.status_code != 200:
                        why.append(f"{name}: variant {r2.status_code}")
                        continue
                    txt, purl = r2.text, bv
                minutes = sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", txt)) / 60
                segs = [l.strip() for l in txt.splitlines() if l.strip() and not l.startswith("#")]
                if not segs:
                    why.append(f"{name}: empty playlist")
                    continue
                r3 = get(urljoin(purl, segs[0]), h, stream=True)
                ok_seg = r3.status_code in (200, 206)
                r3.close()
                if not ok_seg:
                    why.append(f"{name}: first segment {r3.status_code}")
                    continue
                if minutes < MIN_MIN:
                    return {"ok": False, "why": f"only {minutes:.1f} min (decoy/preview?)", "minutes": minutes}
                return {"ok": True, "kind": "hls", "url": purl, "master": url, "headers": h, "minutes": round(minutes, 1), "hdr_variant": name}
            h2 = dict(h)
            h2["Range"] = "bytes=0-1023"
            r = get(url, h2, stream=True)
            total = int((r.headers.get("Content-Range") or "/0").split("/")[-1] or 0) or int(r.headers.get("Content-Length") or 0)
            ct = (r.headers.get("Content-Type") or "").lower()
            r.close()
            if r.status_code not in (200, 206) or ("video" not in ct and "octet" not in ct):
                why.append(f"{name}: {r.status_code} {ct}")
                continue
            if total < max(2 << 20, MIN_MIN * 60 * 60000):
                return {"ok": False, "why": f"file too small ({total >> 20} MB)", "minutes": 0}
            return {"ok": True, "kind": "mp4", "url": url, "headers": h, "minutes": 0, "size": total, "hdr_variant": name}
        except Exception as e:
            why.append(f"{name}: {type(e).__name__}")
    return {"ok": False, "why": "; ".join(why) or "no header set worked"}


class Meter:
    def __init__(self, total):
        self.total, self.n, self.bytes, self.t0, self.last = total, 0, 0, time.time(), 0.0
        self.lock = threading.Lock()

    def add(self, size):
        with self.lock:
            self.n += 1
            self.bytes += size
            now = time.time()
            if now - self.last > 0.5 or self.n >= self.total:
                self.last = now
                print(f"\r   {self.n}/{self.total}  {self.bytes / 1e6:.0f} MB  {self.bytes / max(now - self.t0, .01) / 1e6:.1f} MB/s   ", end="", flush=True)


def ffmpeg_ok():
    if not shutil.which("ffmpeg"):
        log("   ffmpeg not found on PATH (needed for HLS). Install it from https://ffmpeg.org")
        return False
    return True


def download_hls(url, h, out, workers):
    if not ffmpeg_ok():
        return False
    txt = get(url, h).text
    tmp = out + ".parts"
    os.makedirs(tmp, exist_ok=True)
    jobs, lines, n = [], [], 0
    for raw in txt.splitlines():
        s = raw.strip()
        if s.startswith(("#EXT-X-KEY", "#EXT-X-MAP")):
            m = re.search(r'URI="([^"]+)"', s)
            if m:
                n += 1
                name = f"aux{n}" + (".mp4" if s.startswith("#EXT-X-MAP") else ".key")
                jobs.append((urljoin(url, m.group(1)), name))
                s = s.replace(m.group(1), name)
            lines.append(s)
        elif s and not s.startswith("#"):
            n += 1
            ext = os.path.splitext(urlparse(s).path)[1].lower()
            name = f"seg{n:06d}" + (ext if ext in (".ts", ".m4s", ".mp4", ".aac", ".m4a", ".cmfv", ".cmfa") else ".ts")
            jobs.append((urljoin(url, s), name))
            lines.append(name)
        else:
            lines.append(s)
    meter, store = Meter(len(jobs)), threading.local()

    def one(job):
        u, name = job
        path = os.path.join(tmp, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            meter.add(os.path.getsize(path))
            return True
        for attempt in range(6):
            try:
                if not hasattr(store, "s"):
                    store.s = requests.Session()
                    store.s.trust_env = False
                r = store.s.get(u, headers=h, timeout=(10, 40), stream=True)
                r.raise_for_status()
                size = 0
                with open(path + ".part", "wb") as f:
                    for chunk in r.iter_content(1 << 18):
                        f.write(chunk)
                        size += len(chunk)
                os.replace(path + ".part", path)
                meter.add(size)
                return True
            except Exception:
                time.sleep(min(0.4 * 2 ** attempt, 6))
        return False
    with ThreadPoolExecutor(max_workers=workers) as ex:
        ok = list(ex.map(one, jobs))
    print()
    if not all(ok):
        log(f"   {ok.count(False)} segment(s) failed; partial files kept in {tmp} (running again resumes them)")
        return False
    local = os.path.join(tmp, "local.m3u8")
    open(local, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto,data", "-i", local, "-c", "copy"]
    for extra in (["-bsf:a", "aac_adtstoasc"], []):
        if subprocess.call(base + extra + ["-movflags", "+faststart", out]) == 0:
            shutil.rmtree(tmp, ignore_errors=True)
            return True
    return False


def download_file(url, h, out):
    with get(url, h, stream=True, timeout=30) as r:
        r.raise_for_status()
        total, done, t0 = int(r.headers.get("Content-Length") or 0), 0, time.time()
        with open(out + ".part", "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                print(f"\r   {done >> 20} MB" + (f" / {total >> 20} MB" if total else "") + f"  {done / max(time.time() - t0, .01) / 1e6:.1f} MB/s   ", end="", flush=True)
    print()
    os.replace(out + ".part", out)
    return True


# ================================================================= browser download (lulustream's own method)
def join_parts(tmp, out):
    parts = sorted(f for f in os.listdir(tmp) if f.endswith(".part"))
    joined = os.path.join(tmp, "all.bin")
    with open(joined, "wb") as w:
        for f in parts:
            with open(os.path.join(tmp, f), "rb") as r:
                shutil.copyfileobj(r, w)
    for extra in (["-bsf:a", "aac_adtstoasc"], []):
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", joined, "-c", "copy"] + extra + ["-movflags", "+faststart", out], capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            shutil.rmtree(tmp, ignore_errors=True)
            return True
    log("      ffmpeg could not join the segments:", (r.stderr or "").strip()[:200])
    shutil.rmtree(tmp, ignore_errors=True)
    return False


def browser_download(page, frame, url, out, why_out):
    """segments are fetched by JavaScript running inside the player frame, handed to python, joined by ffmpeg. -> True/False"""
    if not ffmpeg_ok():
        return False
    parts = os.path.abspath(out) + ".parts"
    os.makedirs(parts, exist_ok=True)
    have = sorted(int(f[:7]) - 1 for f in os.listdir(parts) if f.endswith(".part") and f[:7].isdigit())
    job = {"dir": parts, "bytes": 0}
    JOBS[page] = job
    for f in page.frames:
        try:
            f.evaluate(JS_PAUSE)          # the tab's own player must not compete for the connection
        except Exception:
            pass
    cfg = {"url": url, "conc": opt("dl-conc", 4, int), "have": have, "retries": 8, "backoff": 0.5, "minsec": MIN_MIN * 60}
    try:
        frame.evaluate("(cfg) => { (" + JS_HLS_DL + ")(cfg); }", cfg)
    except Exception as e:
        why_out.append("could not start in-player download: " + str(e).splitlines()[0])
        return False
    if have:
        log(f"      resuming: {len(have)} segment(s) already saved")
    last_n, last_t, last_print, t0 = -1, time.time(), 0.0, time.time()
    try:
        while True:
            page.wait_for_timeout(1000)           # also lets python receive the segments (exposed binding)
            try:
                st = frame.evaluate("() => window.__mmp")
            except Exception:
                why_out.append("the player frame closed during the download")
                return False
            if not st:
                why_out.append("download script not running")
                return False
            if st.get("err"):
                why_out.append(st["err"])
                return False
            now = time.time()
            if st["done"] != last_n:
                last_n, last_t = st["done"], now
            elif now - last_t > 60:
                why_out.append("no progress for 60 s")
                return False
            if now - last_print > 8 and st.get("total"):
                last_print = now
                slow = f", CDN dropped {st['fails']} request(s) -> {st['limit']} parallel" if st.get("fails") else ""
                log(f"      {st['done']}/{st['total']} segments, {job['bytes'] / 1e6:.0f} MB this session{slow}")
            if st["state"] == "done":
                break
    finally:
        JOBS.pop(page, None)
    ok = join_parts(parts, out)
    if not ok:
        why_out.append("joining failed")
    else:
        mb, dt = os.path.getsize(out) / 1e6, max(time.time() - t0, .001)
        log(f"      in-player download: {mb:.0f} MB in {dt:.0f}s ({mb / dt:.1f} MB/s)")
    return ok


# ================================================================= one step of the plan
def step_mega(ctx, c, out, att):
    for blocked in (True, False):
        ADBLOCK[0] = blocked
        try:
            res, retry = _step_mega(ctx, c, out, att)
        finally:
            ADBLOCK[0] = True
        if res or not retry:
            return res
        log("      anti-adblock wall and nothing caught -> retrying this host once without blocking (ad popups are still closed)")
        att["retried_without_adblock"] = True
    return None


def _step_mega(ctx, c, out, att):
    """-> (result | None, retry_without_blocking)"""
    page = ctx.new_page()
    cat = Catcher(page)
    info = {}
    try:
        try:
            page.goto(c["embed"], wait_until="domcontentloaded", timeout=60000, referer=MEGA_REFERER)
        except Exception as e:
            att["goto_error"] = str(e).splitlines()[0]
        att["stopped_because"] = wait_media(page, cat)
        att["waited_s"] = round(time.time() - cat.t0, 1)
        if not cat.seen:
            att["result"] = "no media"
            wall = wall_seen(page)
            att["adblock_wall"] = wall
            log(f"      nothing caught ({att['stopped_because']}, {att['waited_s']}s)" + ("  [anti-adblock wall shown]" if wall else ""))
            return None, wall and ADBLOCK[0]
        if c["method"] == "browser":                 # lulustream: no python check, the browser itself plays and downloads
            hls = [u for u in sorted(cat.seen, key=rank) if re.search(r"\.m3u8|master|/hls", u.lower()) and (cat.seen[u]["status"] == 200 or cat.seen[u].get("static"))]
            if not hls:
                att["result"] = "browser got no playlist"
                log("      the browser did not receive an HLS playlist")
                return None, False
            link = hls[0]
            log(f"      browser caught {link[:90]}")
            att["link"] = link
            if GRAB_ONLY:
                att["result"] = "ok (browser)"
                return {"kind": "hls", "url": link}, False
            why = []
            if browser_download(page, cat.seen[link]["frame"], link, out, why):
                att["result"] = "ok (browser download)"
                return {"kind": "hls", "url": link, "file": out}, False
            att["result"] = "browser download failed: " + "; ".join(why)
            log("      browser download failed:", "; ".join(why))
            return None, False
        return verify_and_download(cat, c["embed"], out, att), False
    finally:
        close(page)


def verify_and_download(cat, embed, out, att):
    for u in sorted(cat.seen, key=rank)[:3]:
        v = verify(u, cat.seen[u]["headers"], embed, cat.seen[u].get("frame_url"))
        att.setdefault("checks", []).append({"url": u, **{k: v.get(k) for k in ("ok", "why", "kind", "minutes", "hdr_variant")}})
        if not v["ok"]:
            log(f"      rejected {u[:80]}: {v.get('why')}")
            continue
        log(f"      OK {v['kind']} {v['minutes']} min (headers: {v['hdr_variant']})")
        att["link"] = v.get("master") or v["url"]
        if GRAB_ONLY:
            att["result"] = "ok"
            return {"kind": v["kind"], "url": att["link"]}
        try:
            done = download_hls(v["url"], v["headers"], out, WORKERS) if v["kind"] == "hls" else download_file(v["url"], v["headers"], out)
        except Exception as e:
            done = False
            att["download_error"] = str(e).splitlines()[0]
        if done:
            att["result"] = "ok"
            return {"kind": v["kind"], "url": att["link"], "file": out}
        att["result"] = "download failed"
        log("      download failed")
        return None
    att["result"] = att.get("result") or "python cannot use the link"
    return None


def step_outer(ctx, play_url, name, out, att):
    """a server of the AnimeZid page itself (Uqload, DoodStream, VidTube ...): click its button and watch the whole page"""
    for blocked in (True, False):
        ADBLOCK[0] = blocked
        page, items = open_play(ctx, play_url)
        try:
            loc = next((l for l, n in items if n.lower() == name.lower()), None)
            if loc is None:
                att["result"] = "button not found"
                return None
            cat = Catcher(page)
            try:
                loc.click(timeout=5000)
            except Exception:
                try:
                    loc.dispatch_event("click")
                except Exception as e:
                    att["result"] = "click failed"
                    return None
            att["stopped_because"] = wait_media(page, cat)
            att["waited_s"] = round(time.time() - cat.t0, 1)
            if cat.seen:
                return verify_and_download(cat, page.url, out, att)
            log(f"      nothing caught ({att['stopped_because']}){' - retrying without ad blocking' if blocked else ''}")
        finally:
            close(page)
            ADBLOCK[0] = True
    att["result"] = "no media"
    return None


# ================================================================= one video
def process(ctx, src, label, report, sub=""):
    rec = {"src": src, "label": label, "attempts": [], "ok": False}
    report.append(rec)
    log(f"\n=== {label or src}")
    play_url, mega_url, outer, title = None, None, [], ""
    if re.search(r"megamax|share4max", urlparse(src).netloc) or re.search(r"/iframe/[A-Za-z0-9]{8,}/?$", urlparse(src).path):
        mega_url = src
    else:
        play_url = play_url_of(src)
        log("   opening the AnimeZid page ...")
        mega_url, outer, title = find_megamax(ctx, play_url)
        if NO_OUTER:
            outer = []
        log("   MegaMax:", mega_url or "(not found)", "| other servers on the page:", ", ".join(outer) or "-")
    mega, name = [], ""
    if mega_url:
        try:
            streams, name = get_mirrors(ctx, mega_url)
            mega = build_candidates(streams)
        except Exception as e:
            rec["mirror_error"] = str(e)
            log("   MegaMax mirrors:", e)
    label = label or ep_number(title, name) or safe_name(name) or safe_name(title) or "video"
    rec["label"] = label
    out = os.path.join(OUT, sub, safe_name(label) + ".mp4")
    if os.path.exists(out) and os.path.getsize(out) > 5 << 20 and not GRAB_ONLY:
        log("   already downloaded:", out)
        rec.update(ok=True, file=out, skipped=True)
        return rec
    plan = build_plan(mega, outer if play_url else [])
    log("   plan:", " > ".join(f"{s['driver']}" + (f"({s['height']}p)" if s["height"] else "") + ("*" if s["method"] == "browser" else "") for s in plan) or "(nothing to try)")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    for s in plan:
        log(f"   -> {s['driver']} {s['label']}" + (f"  {s['embed']}" if s.get("embed") else "  (page server)") + ("   [in-player method]" if s["method"] == "browser" else ""))
        att = {"type": s["type"], "driver": s["driver"], "quality": s["label"], "embed": s.get("embed"), "method": s["method"]}
        rec["attempts"].append(att)
        try:
            res = step_mega(ctx, s, out, att) if s["type"] == "mega" else step_outer(ctx, play_url, s["driver"], out, att)
        except Exception as e:
            att["error"] = str(e).splitlines()[0] if str(e) else repr(e)
            log("      error:", att["error"])
            res = None
        if res:
            rec.update(ok=True, link=res.get("url"), kind=res.get("kind"), host=s["driver"], file=res.get("file"))
            if res.get("file"):
                log(f"      saved: {res['file']}")
            return rec
    log("   all servers failed for this video")
    return rec


def read_links(path):
    jobs = []
    for ln in open(path, encoding="utf-8-sig"):
        m = re.search(r"https?://[^\s,]+", ln)
        if m:
            jobs.append((ln.split(" , ")[0].strip() if " , " in ln else "", m.group(0)))
    return jobs


def main():
    from playwright.sync_api import sync_playwright
    jobs = [("", a) for a in sys.argv[1:] if a.startswith("http")]
    f = opt("file", "")
    if f:
        jobs += read_links(f)
    if not jobs:
        sys.exit(__doc__)
    nm = opt("name", "")
    if nm and len(jobs) == 1:
        jobs[0] = (nm, jobs[0][1])
    report = []
    with sync_playwright() as p:
        b, ctx = new_context(p)
        try:
            expanded = []
            for lab, u in jobs:
                pu = urlparse(u)
                if "animezid" in pu.netloc and re.match(r"/series/", unquote(pu.path)):
                    log(f"collecting episodes of {u} ...")
                    for e in collect_episodes(ctx, u):
                        expanded.append((e["label"], e["href"], e["sub"]))
                    log(f"   {len(expanded)} episode(s) selected")
                else:
                    expanded.append((lab, u, ""))
            for lab, u, sub in expanded:
                process(ctx, u, lab, report, sub)
                json.dump(report, open("mmgrab_report.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        finally:
            b.close()
    ok = sum(1 for r in report if r["ok"])
    log(f"\nfinished: {ok}/{len(report)} ok   (details: mmgrab_report.json)")


if __name__ == "__main__":
    main()
