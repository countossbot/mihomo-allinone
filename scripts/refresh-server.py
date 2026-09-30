#!/usr/bin/env python3
# ============================================================
# 节点刷新 + 订阅管理服务
#
# 接口：
#   GET  /                    管理页面
#   GET  /status              运行状态
#   GET  /health              健康检查
#   GET  /config              读取当前设置（订阅/白名单/阈值）
#   POST /config              保存设置（写入 settings.json）
#   POST /refresh             执行刷新
#   GET  /result              上次刷新明细（保留/剔除节点）
#
# 设置持久化：/data/settings.json
# ============================================================
import os, json, time, shutil, subprocess, threading, tempfile, urllib.parse, urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

WORKDIR = os.environ.get("WORKDIR", "/app")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "8765"))
SECRET = os.environ.get("MIHOMO_SECRET", "") or "change-me-before-expose"
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

DEFAULT_SETTINGS = {
    # 订阅地址：优先取环境变量 SUB_URLS（compose/.env 注入），
    # 其次由管理页写入 /data/settings.json。
    # 此处不硬编码，避免凭证进入版本库。
    "sub_urls": [u.strip() for u in os.environ.get("SUB_URLS", "").split(",") if u.strip()],
    "region_whitelist": os.environ.get("REGION_WHITELIST", "DE NL GB US JP SG FI DK"),
    "max_delay": int(os.environ.get("MAX_DELAY", "3000")),
    "test_timeout": int(os.environ.get("TEST_TIMEOUT", "3000")),
    "strategy": os.environ.get("STRATEGY", "sticky-sessions"),
}

MIHOMO = "/usr/local/bin/mihomo"
_state = {"running": False, "last_run": None, "last_result": "尚未执行", "log": []}
_last_result = {}


# ---------------- 设置读写 ----------------
def load_settings():
    s = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        try:
            s.update(json.load(open(SETTINGS_FILE, encoding="utf-8")))
        except Exception:
            pass
    if not s.get("sub_urls"):
        s["sub_urls"] = DEFAULT_SETTINGS["sub_urls"]
    return s


def save_settings(s):
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(s, open(SETTINGS_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def load_yaml(p):
    import yaml
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def dump_yaml(o, p):
    import yaml
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(o, f, allow_unicode=True, sort_keys=False)


# ---------------- 刷新流水线 ----------------
def pipeline():
    global _last_result
    _state.update(running=True, log=[])
    t0 = time.time()
    cfg = load_settings()
    tmp = tempfile.mkdtemp(prefix="refresh-", dir="/tmp")

    try:
        urls = cfg["sub_urls"]
        regions = [r for r in cfg["region_whitelist"].split() if r]
        max_delay = int(cfg["max_delay"])
        tmo = int(cfg["test_timeout"])
        strategy = cfg.get("strategy", "sticky-sessions")

        def region_ok(name):
            return any(name == r or name.startswith(r + " ") for r in regions)

        # ---------- 1. 拉订阅 ----------
        _state["log"].append(f"▶ 拉取 {len(urls)} 个订阅…")
        prov = {}
        for i, u in enumerate(urls, 1):
            prov[f"sub{i}"] = {"type": "http", "url": u,
                               "path": os.path.join(tmp, f"sub{i}.yaml"),
                               "interval": 3600}
        fetch_cfg = {"mixed-port": 7890, "external-controller": "127.0.0.1:9099",
                     "secret": "fetcher", "log-level": "silent",
                     "proxy-providers": prov,
                     "proxy-groups": [{"name": "g", "type": "select",
                                       "use": list(prov.keys())}],
                     "rules": ["MATCH,g"]}
        dump_yaml(fetch_cfg, os.path.join(tmp, "fetch.yaml"))

        p = subprocess.Popen([MIHOMO, "-d", tmp, "-f", os.path.join(tmp, "fetch.yaml")],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(90):
            if all(os.path.exists(os.path.join(tmp, f"sub{i}.yaml")) and
                   os.path.getsize(os.path.join(tmp, f"sub{i}.yaml")) > 0
                   for i in range(1, len(urls) + 1)):
                break
            time.sleep(1)
        time.sleep(2)
        p.terminate()
        try: p.wait(timeout=5)
        except Exception: p.kill()

        # ---------- 2. 白名单 + 前缀 ----------
        _state["log"].append(f"▶ 白名单过滤: {cfg['region_whitelist']}")
        cands = []
        for i in range(1, len(urls) + 1):
            fp = os.path.join(tmp, f"sub{i}.yaml")
            if not os.path.exists(fp) or os.path.getsize(fp) == 0:
                _state["log"].append(f"   ⚠️ 订阅{i} 拉取失败")
                continue
            d = load_yaml(fp) or {}
            for n in (d.get("proxies") or []):
                nm = n.get("name", "")
                if not region_ok(nm):
                    continue
                node = dict(n)
                if i > 1:
                    node["name"] = f"S{i}-{nm}"
                cands.append({"src": f"sub{i}", "node": node})
        _state["log"].append(f"   候选节点: {len(cands)}")
        if not cands:
            _state["log"].append("❌ 无可用节点")
            _state["last_result"] = "无节点"
            return

        # ---------- 3. 测速 ----------
        _state["log"].append(f"▶ 测速（阈值 {max_delay}ms）…")
        tcfg = {"mixed-port": 7891, "external-controller": "127.0.0.1:9099",
                "secret": "tester", "log-level": "silent",
                "proxies": [c["node"] for c in cands],
                "proxy-groups": [{"name": "g", "type": "select",
                                  "proxies": [c["node"]["name"] for c in cands] + ["DIRECT"]}],
                "rules": ["MATCH,g"]}
        dump_yaml(tcfg, os.path.join(tmp, "test.yaml"))
        tp = subprocess.Popen([MIHOMO, "-d", tmp, "-f", os.path.join(tmp, "test.yaml")],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(8)

        kept, dropped = [], []
        for c in cands:
            nm = c["node"]["name"]
            q = urllib.parse.urlencode({"url": "http://www.gstatic.com/generate_204",
                                        "timeout": tmo})
            u = f"http://127.0.0.1:9099/proxies/{urllib.parse.quote(nm)}/delay?{q}"
            req = urllib.request.Request(u, headers={"Authorization": "Bearer tester"})
            try:
                d = json.loads(urllib.request.urlopen(req, timeout=tmo / 1000 + 5).read())
                delay = int(d.get("delay", 0) or 0)
            except Exception:
                delay = 0
            if delay and 0 < delay <= max_delay:
                c["delay"] = delay; kept.append(c)
            else:
                dropped.append(c)

        tp.terminate()
        try: tp.wait(timeout=5)
        except Exception: tp.kill()

        kept.sort(key=lambda x: x.get("delay", 9999))
        _state["log"].append(f"   保留 {len(kept)} / 剔除 {len(dropped)}")
        if kept:
            _state["log"].append(f"   最优: {kept[0]['node']['name']} ({kept[0]['delay']}ms)")
        if not kept:
            _state["log"].append("❌ 全部超时")
            _state["last_result"] = "全部超时"
            return

        # ---------- 4. 生成配置 ----------
        _state["log"].append("▶ 生成配置…")
        names = [c["node"]["name"] for c in kept]
        groups = {}
        for c in kept:
            groups.setdefault(c["src"].upper(), []).append(c["node"]["name"])

        out = {
            "mixed-port": 7890, "allow-lan": True,
            "external-controller": "0.0.0.0:9090", "secret": SECRET,
            "mode": "rule", "log-level": "info", "ipv6": True,
            "geodata-mode": True,
            "geox-url": {"geoip": "", "mmdb": "", "geosite": ""},
            "dns": {"enable": True, "enhanced-mode": "fake-ip",
                    "fake-ip-range": "198.18.0.1/16",
                    "fake-ip-filter": ["*.lan", "*.local"],
                    "nameserver": ["https://223.5.5.5/dns-query",
                                   "https://1.1.1.1/dns-query"]},
            "proxies": [c["node"] for c in kept],
            "proxy-groups": [],
            "rules": ["GEOIP,LAN,DIRECT,no-resolve", "GEOIP,CN,DIRECT",
                      "MATCH,🚀 节点选择"],
        }
        g = out["proxy-groups"]
        tags = sorted(groups.keys())
        for tag in tags:
            lst = groups[tag]
            g.append({"name": f"♻️ 测速-{tag}", "type": "url-test",
                      "url": "http://www.gstatic.com/generate_204",
                      "interval": 300, "timeout": 3000, "tolerance": 50,
                      "proxies": lst})
            g.append({"name": f"🔮 负载均衡-{tag}", "type": "load-balance",
                      "url": "http://www.gstatic.com/generate_204",
                      "interval": 300, "timeout": 3000,
                      "strategy": strategy, "proxies": lst})
        msrc = [f"♻️ 测速-{t}" for t in tags]
        lsrc = [f"🔮 负载均衡-{t}" for t in tags]
        top = []
        if len(msrc) > 1:
            g.append({"name": "♻️ 自动选择", "type": "url-test",
                      "url": "http://www.gstatic.com/generate_204",
                      "interval": 300, "timeout": 3000, "tolerance": 50,
                      "proxies": msrc})
            g.append({"name": "🔮 负载均衡", "type": "load-balance",
                      "url": "http://www.gstatic.com/generate_204",
                      "interval": 300, "timeout": 3000,
                      "strategy": strategy, "proxies": lsrc})
            g.append({"name": "🔯 故障转移", "type": "fallback",
                      "url": "http://www.gstatic.com/generate_204",
                      "interval": 300, "timeout": 3000, "proxies": msrc})
            top = ["♻️ 自动选择", "🔮 负载均衡", "🔯 故障转移"]
        g.append({"name": "🚀 节点选择", "type": "select",
                  "proxies": top + msrc + lsrc + ["DIRECT"]})

        cfg_path = os.path.join(DATA_DIR, "config.yaml")
        dump_yaml(out, cfg_path)
        _state["log"].append(f"   已写入 config.yaml（{len(kept)} 节点）")

        _last_result = {
            "generated_at": time.strftime("%F %T"),
            "kept": [{"name": c["node"]["name"], "delay": c.get("delay"),
                      "src": c["src"]} for c in kept],
            "dropped": [{"name": c["node"]["name"], "src": c["src"]} for c in dropped],
        }
        json.dump(_last_result, open(os.path.join(DATA_DIR, "nodes-filtered.json"),
                                     "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

        # ---------- 5. 重启内核 ----------
        _state["log"].append("▶ 重启内核…")
        r = subprocess.run(["supervisorctl", "restart", "mihomo"],
                           capture_output=True, text=True, timeout=120)
        s = (r.stdout + r.stderr).strip()
        _state["log"].append(s or "已重启")
        ok = "ERROR" not in s.upper()
        _state["last_result"] = "成功" if ok else "重启失败"
        _state["log"].append(f"{'✅' if ok else '❌'} 完成 耗时 {int(time.time()-t0)}s")

    except subprocess.TimeoutExpired:
        _state["log"].append("❌ 超时"); _state["last_result"] = "超时"
    except Exception as e:
        _state["log"].append(f"❌ 异常: {e}"); _state["last_result"] = "异常"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _state["running"] = False
        _state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 页面 ----------------
PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>订阅管理 · mihomo</title>
<style>
 :root{color-scheme:dark;--bg:#0a0a0b;--card:#151517;--bd:#26262a;
       --fg:#e8e8ea;--dim:#71717a;--pri:#2563eb;--ok:#22c55e;--err:#ef4444}
 *{box-sizing:border-box}
 body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      background:var(--bg);color:var(--fg);margin:0;padding:24px 16px;
      display:flex;justify-content:center}
 .wrap{width:100%;max-width:680px}
 h1{font-size:19px;margin:0 0 4px;font-weight:600}
 .sub{color:var(--dim);font-size:12.5px;margin:0 0 20px}
 .card{background:var(--card);border:1px solid var(--bd);border-radius:14px;
       padding:20px 22px;margin-bottom:14px}
 .card h2{font-size:13px;margin:0 0 14px;color:var(--dim);font-weight:600;
          text-transform:uppercase;letter-spacing:.6px}
 label{display:block;font-size:12px;color:var(--dim);margin:12px 0 6px}
 input,select{width:100%;background:#0d0d0f;border:1px solid var(--bd);color:var(--fg);
      padding:10px 12px;border-radius:8px;font-size:13px;font-family:inherit;
      transition:border-color .15s}
 input:focus,select:focus{outline:0;border-color:var(--pri)}
 .row{display:flex;gap:10px;align-items:flex-end}
 .row>div{flex:1}
 button{background:var(--pri);color:#fff;border:0;padding:11px 20px;
        border-radius:9px;font-size:14px;font-weight:500;cursor:pointer;
        font-family:inherit;transition:.15s}
 button:hover:not(:disabled){background:#1d4ed8}
 button:disabled{background:#232326;color:#4b4b52;cursor:not-allowed}
 button.sec{background:#232326;color:var(--fg)}
 button.sec:hover:not(:disabled){background:#2e2e33}
 button.sm{padding:7px 14px;font-size:12.5px}
 .subitem{display:flex;gap:8px;margin-bottom:8px;align-items:center}
 .subitem input{flex:1}
 .subitem button{background:#3f1d1d;color:#fca5a5;padding:9px 13px}
 .subitem button:hover{background:#5c2626}
 #out{margin-top:12px;padding:13px 15px;background:#0d0d0f;border:1px solid var(--bd);
      border-radius:9px;font-family:ui-monospace,Menlo,monospace;font-size:11.5px;
      line-height:1.75;color:#8b8b93;white-space:pre-wrap;max-height:340px;
      overflow-y:auto;display:none}
 .dot{display:inline-block;width:7px;height:7px;border-radius:50%;
      background:#3f3f46;margin-right:8px;vertical-align:middle}
 .dot.on{background:var(--ok);animation:p 1.2s infinite}
 @keyframes p{0%,100%{opacity:1}50%{opacity:.3}}
 .toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
        background:#1e3a2a;border:1px solid #2f5f3f;color:#86efac;
        padding:11px 22px;border-radius:9px;font-size:13px;opacity:0;
        transition:opacity .25s;pointer-events:none}
 .toast.show{opacity:1}
 .toast.err{background:#3f1d1d;border-color:#5c2626;color:#fca5a5}
 .meta{font-size:11px;color:#4b4b52;margin-top:10px}
 table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
 th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #1f1f23}
 th{color:var(--dim);font-weight:500;font-size:11px}
 .pill{padding:2px 8px;border-radius:20px;font-size:10.5px;background:#1f2a3d;color:#93c5fd}
 .pill.g{background:#1e3a2a;color:#86efac}
</style></head><body>
<div class="wrap">
  <h1><span class="dot" id="d"></span>订阅管理</h1>
  <p class="sub">管理订阅源 · 地区过滤 · 延迟阈值 · 一键刷新节点</p>

  <!-- 订阅源 -->
  <div class="card">
    <h2>订阅源</h2>
    <div id="subs"></div>
    <button class="sec sm" onclick="addSub()">+ 添加订阅</button>
    <div class="meta">多个订阅会自动合并，第二个起加 S2-/S3- 前缀防重名</div>
  </div>

  <!-- 筛选设置 -->
  <div class="card">
    <h2>筛选设置</h2>
    <label>地区白名单（空格分隔，留空=全部）</label>
    <input id="wl" placeholder="DE NL GB US JP SG FI DK">
    <div class="row" style="margin-top:12px">
      <div>
        <label>延迟阈值 (ms)</label>
        <input id="md" type="number" value="3000">
      </div>
      <div>
        <label>测速超时 (ms)</label>
        <input id="tt" type="number" value="3000">
      </div>
    </div>
    <label>负载均衡策略</label>
    <select id="st">
      <option value="sticky-sessions">sticky-sessions（会话保持，推荐）</option>
      <option value="round-robin">round-robin（每连接换 IP）</option>
      <option value="consistent-hashing">consistent-hashing（同域名固定）</option>
    </select>
  </div>

  <!-- 操作 -->
  <div class="card">
    <h2>操作</h2>
    <div class="row">
      <button onclick="save()">保存设置</button>
      <button id="rb" onclick="refresh()">保存并刷新</button>
    </div>
    <div id="out"></div>
    <div class="meta" id="m"></div>
  </div>

  <!-- 结果 -->
  <div class="card" id="resCard" style="display:none">
    <h2>上次筛选结果 <span id="resTime" class="meta"></span></h2>
    <div id="res"></div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let cfg = null;

function toast(msg, err){
  const t = $('#toast'); t.textContent = msg;
  t.className = 'toast show' + (err ? ' err' : '');
  setTimeout(()=>t.className='toast', 2600);
}

function renderSubs(list){
  const box = $('#subs'); box.innerHTML = '';
  list.forEach((u,i)=>{
    const d = document.createElement('div'); d.className='subitem';
    const inp = document.createElement('input');
    inp.value = u; inp.placeholder = 'https://.../sub?token=...';
    inp.oninput = ()=>{ cfg.sub_urls[i] = inp.value; };
    const btn = document.createElement('button');
    btn.textContent = '删除'; btn.className='sm';
    btn.onclick = ()=>{ cfg.sub_urls.splice(i,1); renderSubs(cfg.sub_urls); };
    d.append(inp, btn); box.append(d);
  });
}

function addSub(){ cfg.sub_urls.push(''); renderSubs(cfg.sub_urls); }

async function load(){
  cfg = await (await fetch('/config')).json();
  renderSubs(cfg.sub_urls);
  $('#wl').value = cfg.region_whitelist || '';
  $('#md').value = cfg.max_delay;
  $('#tt').value = cfg.test_timeout;
  $('#st').value = cfg.strategy || 'sticky-sessions';
  loadResult();
}

function collect(){
  cfg.sub_urls = cfg.sub_urls.filter(u=>u.trim());
  cfg.region_whitelist = $('#wl').value.trim();
  cfg.max_delay = parseInt($('#md').value)||3000;
  cfg.test_timeout = parseInt($('#tt').value)||3000;
  cfg.strategy = $('#st').value;
  return cfg;
}

async function save(){
  const c = collect();
  if(!c.sub_urls.length){ toast('至少保留一个订阅', true); return false; }
  const r = await fetch('/config', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(c)});
  if(r.ok){ toast('设置已保存'); return true; }
  toast('保存失败', true); return false;
}

async function refresh(){
  if(!await save()) return;
  const b=$('#rb'); b.disabled=true; b.textContent='刷新中…';
  $('#out').style.display='block'; $('#out').textContent='启动…';
  try{ await fetch('/refresh',{method:'POST'}); }catch(e){}
  st();
}

function showLog(lines){
  const o=$('#out'); o.style.display='block';
  o.textContent = lines.join('\\n'); o.scrollTop=o.scrollHeight;
}

async function st(){
  try{
    const j = await (await fetch('/status')).json();
    $('#d').className = 'dot' + (j.running?' on':'');
    if(j.running){ showLog(j.log); $('#rb').disabled=true; $('#rb').textContent='刷新中…'; }
    else { $('#rb').disabled=false; $('#rb').textContent='保存并刷新'; }
    $('#m').textContent = j.last_run ? ('上次 '+j.last_run+' · '+j.last_result) : '';
    if(!j.running) loadResult();
  }catch(e){}
}

async function loadResult(){
  try{
    const j = await (await fetch('/result')).json();
    if(!j.kept){ $('#resCard').style.display='none'; return; }
    $('#resCard').style.display='block';
    $('#resTime').textContent = j.generated_at || '';
    let html = '<table><tr><th>节点</th><th>延迟</th><th>来源</th></tr>';
    j.kept.slice(0,40).forEach(k=>{
      const src = k.src || (k.name.startsWith('S2-') ? 'sub2' :
                            k.name.startsWith('S3-') ? 'sub3' : 'sub1');
      html += `<tr><td>${k.name}</td><td>${k.delay}ms</td>
               <td><span class="pill g">${src}</span></td></tr>`;
    });
    if(j.dropped && j.dropped.length){
      html += `<div class="meta" style="margin-top:14px">已剔除 ${j.dropped.length} 个（超时/高延迟）：</div>`;
      html += '<div style="font-size:11.5px;color:#71717a;margin-top:6px">'
            + j.dropped.map(d=>d.name).join('、') + '</div>';
    }
    $('#res').innerHTML = html;
  }catch(e){}
}

load();
setInterval(st, 2500);
</script></body></html>
"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _s(self, code, body, ct="text/plain; charset=utf-8"):
        d = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def _json(self, obj, code=200):
        self._s(code, json.dumps(obj, ensure_ascii=False),
                "application/json; charset=utf-8")

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/health":      self._s(200, "ok")
        elif p == "/status":    self._json(_state)
        elif p == "/config":    self._json(load_settings())
        elif p == "/result":    self._json(_last_result or
                                            (json.load(open(os.path.join(DATA_DIR,"nodes-filtered.json"),encoding="utf-8"))
                                             if os.path.exists(os.path.join(DATA_DIR,"nodes-filtered.json")) else {}))
        else:                   self._s(200, PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        p = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        if p == "/config":
            try:
                cfg = json.loads(self.rfile.read(n) or b"{}")
                save_settings(cfg)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif p == "/refresh":
            if _state["running"]:
                self._s(409, "已有任务在运行"); return
            threading.Thread(target=pipeline, daemon=True).start()
            time.sleep(0.3)
            self._s(200, "任务已启动")
        else:
            self._s(404, "not found")


if __name__ == "__main__":
    s = load_settings()
    print(f"[refresh] :{PORT} 订阅数={len(s['sub_urls'])} 白名单={s['region_whitelist']}", flush=True)
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
