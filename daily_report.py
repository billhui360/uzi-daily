#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UZI 每日选股 · 云端自包含版 —— GitHub Actions 跑，baostock 数据，SMTP 发邮件.

策略（经 UZI 回测样本外验证）：
  范围：中证500 + 中证1000 成分股（≈1500 中盘）
  入场：月线上升趋势 Stage2（来自大底 / 走出底部 / 月线多头 / 低点抬高 / 量能）
  增强：① 筹码集中（股东户数较 2 季前下降 = 主力吸筹，加分）
        ② 大盘择时（沪深300 跌破 MA10 = 熊市，邮件顶部红色警示）
        ③ ③逼近前高优先排序（回测里该阶段最强）
  另出：自选股每日体检（watchlist.txt）

需要的 GitHub Secrets：
  SMTP_HOST（如 smtp.qq.com）· SMTP_PORT（默认 465）· SMTP_USER · SMTP_PASS（授权码）
  MAIL_TO（逗号分隔）· MAIL_FROM（默认 = SMTP_USER）
本地测试不发信：DRY_RUN=1 python3 daily_report.py
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
import time
from datetime import date, timedelta
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

# ─────────── 策略参数 ───────────
CFG = dict(
    base_lb=48,        # 找多年绝对底的回看窗口(月)
    deep_dd=0.45,      # 底相对之前高点的最小跌幅(困境反转背景)
    min_off=4,         # 底至少在几个月前
    min_lift=0.15,     # 现价较底最小抬升(无上限)
    mid_ma=12,         # 趋势均线(月线12=年线)
    slope_lb=3,        # 均线上行判断回看月
    hl_margin=0.06,    # 低点抬高最小幅度
    vol_mult=0.80,     # 量能软门槛
    ceiling_lb=84,     # 历史高点回看窗口(月)
    min_months=30, min_price=3.0,
)
STAGE_ORDER = {"③ 逼近前高": 0, "④ 突破新高": 1, "② 中段": 2, "① 刚离底": 3}


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _is_junk(code, name):
    name = str(name or "")
    if any(t in name for t in ("ST", "*ST", "退", "退市")):
        return True
    if code[:1] in ("8", "4") or code[:3] == "920":
        return True
    return False


# ─────────── 核心：月线上升趋势 Stage2 判定（纯函数）───────────
def evaluate_series(code, name, highs, lows, closes, vols, cfg):
    if len(closes) < cfg["min_months"]:
        return None
    n = len(closes)
    last = closes[-1]
    if last < cfg["min_price"] or n <= cfg["mid_ma"] + cfg["slope_lb"]:
        return None
    lb = min(n, cfg["base_lb"])
    seg = lows[-lb:]
    bi = n - lb + seg.index(min(seg))
    blow = lows[bi]
    if blow <= 0:
        return None
    peak_pre = max(highs[:bi + 1]) if bi > 0 else highs[0]
    base_dd = 1 - blow / peak_pre if peak_pre > 0 else 0
    if base_dd < cfg["deep_dd"]:
        return None
    off = (n - 1) - bi
    if off < cfg["min_off"] or last / blow - 1 < cfg["min_lift"]:
        return None
    ma_now = _mean(closes[-cfg["mid_ma"]:])
    ma_prev = _mean(closes[-cfg["mid_ma"] - cfg["slope_lb"]:-cfg["slope_lb"]])
    if not (last > ma_now and ma_now > ma_prev):
        return None
    post = lows[bi:]
    if len(post) < 6:
        return None
    half = len(post) // 2
    el, ll = min(post[:half]), min(post[half:])
    if ll <= el * (1 + cfg["hl_margin"]):
        return None
    rv = _mean(vols[-3:])
    bv = _mean(vols[-15:-3]) if n >= 15 else _mean(vols[:-3])
    vr = (rv / bv) if bv > 0 else 0
    if vr < cfg["vol_mult"]:
        return None
    hh = max(highs[-cfg["ceiling_lb"]:]) if n > cfg["ceiling_lb"] else max(highs)
    to_high = hh / last - 1

    def _c(x, lo=0.0, hi=1.0):
        return max(lo, min(hi, x))
    score = round(100 * (0.30 * _c((ma_now / ma_prev - 1) / 0.20)
                         + 0.22 * _c((ll / el - 1 - cfg["hl_margin"]) / 0.30)
                         + 0.18 * _c((vr - cfg["vol_mult"]) / 2.0)
                         + 0.15 * _c((base_dd - cfg["deep_dd"]) / 0.40)
                         + 0.15 * _c(to_high / 1.0)), 1)
    frac = last / hh if hh > 0 else 1
    stage = ("① 刚离底" if frac < 0.55 else "② 中段" if frac < 0.75
             else "③ 逼近前高" if frac < 1.0 else "④ 突破新高")
    return {"code": code, "name": name, "price": round(last, 2), "阶段": stage,
            "距高点%": round(to_high * 100, 0), "抬底%": round((ll / el - 1) * 100, 0),
            "量能比": round(vr, 2), "底深%": round(base_dd * 100, 0), "score": score}


def _pref(code):
    return "sh." if code[0] == "6" else "sz."


def load_constituents():
    """扫描哪些指数成分。env INDEXES 逗号分隔，默认 000905(中证500)。
    想扫中证500+1000 就设 INDEXES=000905,000852。"""
    import akshare as ak
    idxs = [x.strip() for x in (os.environ.get("INDEXES") or "000905").split(",") if x.strip()]
    codes = {}
    for idx in idxs:
        for _ in range(3):
            try:
                df = ak.index_stock_cons(symbol=idx)
                for c, nm in zip(df["品种代码"], df["品种名称"]):
                    c = str(c).zfill(6)
                    if not _is_junk(c, nm):
                        codes[c] = str(nm)
                break
            except Exception:
                time.sleep(1.0)
    # LIMIT 可再截断（快速试跑）
    lim = int(os.environ.get("LIMIT") or "0")
    items = list(codes.items())
    return items[:lim] if lim > 0 else items


def _sina_monthly(code, attempts=4):
    """新浪日线→合成月线(H/L/C/V)。新浪不限流、不拒海外 IP，GitHub 可用。"""
    import akshare as ak
    sym = ("sh" if code[0] == "6" else "sz") + code
    for i in range(attempts):
        try:
            df = ak.stock_zh_a_daily(symbol=sym, start_date="20120101", adjust="qfq")
            if df is not None and not df.empty:
                ym = df["date"].astype(str).str[:7]
                g = df.groupby(ym)
                return ([float(x) for x in g["high"].max()],
                        [float(x) for x in g["low"].min()],
                        [float(x) for x in g["close"].last()],
                        [float(x) for x in g["volume"].sum()])
        except Exception:
            pass
        time.sleep(0.4 * (i + 1))
    return None


def fetch_monthly(uni):
    """新浪日线合成月线，可并发(新浪扛得住)。WORKERS env 默认 6。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    workers = int(os.environ.get("WORKERS") or "6")

    def one(code, name):
        r = _sina_monthly(code)
        if r is None or len(r[2]) < CFG["min_months"]:
            return None
        return (code, name, r[0], r[1], r[2], r[3])

    data, done = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, c, nm) for c, nm in uni]
        for f in as_completed(futs):
            done += 1
            if done % 100 == 0:
                print(f"  月线 {done}/{len(uni)} · 已取 {len(data)}", file=sys.stderr)
            try:
                r = f.result()
                if r:
                    data[r[0]] = (r[1], r[2], r[3], r[4], r[5])
            except Exception:
                pass
    return data


def market_regime():
    """沪深300 月线 MA10 择时。akshare 新浪指数日线重采样为月末收盘。"""
    import akshare as ak
    try:
        df = ak.stock_zh_index_daily(symbol="sh000300")
        if df is None or df.empty:
            return None
        m = {}
        for _, r in df.iterrows():
            m[str(r["date"])[:7]] = float(r["close"])
        C = [m[k] for k in sorted(m)]
        if len(C) < 10:
            return None
        return {"bull": C[-1] > sum(C[-10:]) / 10}
    except Exception:
        return None


def chip_concentrating(code, nq=2):
    import akshare as ak
    for _ in range(2):
        try:
            df = ak.stock_zh_a_gdhs_detail_em(symbol=code)
            if df is None or df.empty or "股东户数-本次" not in df.columns:
                return None
            ser = [float(x) for x in df["股东户数-本次"].tolist() if x and float(x) > 0]
            if len(ser) <= nq:
                return None
            return ser[-1] < ser[-1 - nq]
        except Exception:
            time.sleep(0.5)
    return None


def run_screen(data):
    hits = [r for code, (name, H, L, C, V) in data.items()
            if (r := evaluate_series(code, name, H, L, C, V, CFG))]
    print(f"  查筹码集中 {len(hits)} 只...", file=sys.stderr)
    for r in hits:
        chip = chip_concentrating(r["code"])
        r["筹码集中"] = chip
        r["score_adj"] = r["score"] + (8 if chip else (-4 if chip is False else 0))
    hits.sort(key=lambda x: (STAGE_ORDER.get(x["阶段"], 9), -x["score_adj"]))
    return hits


def load_watchlist():
    fp = Path(__file__).parent / "watchlist.txt"
    codes = []
    if fp.exists():
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip().split("#")[0].strip()
            if line[:6].isdigit():
                codes.append(line[:6])
    return list(dict.fromkeys(codes))


def watch_status(codes, data):
    rows = []
    for code in codes:
        d = data.get(code)
        if not d:
            rows.append((code, code, "—", "不在中证500/1000 或数据缺"))
            continue
        name, H, L, C, V = d
        r = evaluate_series(code, name, H, L, C, V, CFG)
        if r:
            tag = " · 筹码集中✓" if chip_concentrating(code) else ""
            rows.append((code, name, r["阶段"], f"得分{r['score']} · 距高{r['距高点%']:.0f}%{tag}"))
        else:
            last, ma12 = C[-1], sum(C[-12:]) / min(12, len(C))
            rows.append((code, name, "未入选", "跌破年线/趋势未起" if last <= ma12 else "多头但未满足大底/抬高"))
    return rows


def build_html(hits, watch, regime, top_n):
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")
    P = []
    if regime is not None:
        if regime["bull"]:
            P.append("<div style='background:#e6f4e6;color:#1a7a1a;padding:10px 14px;border-radius:6px;font-size:14px'>🟢 <b>大盘趋势市</b>（沪深300 站上 MA10）· 可参与</div>")
        else:
            P.append("<div style='background:#fbe4e4;color:#b02020;padding:10px 14px;border-radius:6px;font-size:14px'>🔴 <b>大盘转弱</b>（沪深300 跌破 MA10）· <b>建议空仓/谨慎</b>，下面仅供观察</div>")
    P.append(f"<h2 style='margin-top:16px'>📈 月线趋势Stage2+筹码集中 <span style='color:#888;font-size:13px'>（中证500+1000 · 命中 {len(hits)} · Top {min(top_n,len(hits))}）</span></h2>")
    P.append("<table cellpadding=6 style='border-collapse:collapse;font-size:13px'><tr style='background:#f0f0f0'><th>代码</th><th>名称</th><th>现价</th><th>阶段</th><th>筹码</th><th>距高点</th><th>抬底</th><th>量能</th><th>得分</th></tr>")
    for r in hits[:top_n]:
        chip = "✓吸筹" if r.get("筹码集中") else ("✗分散" if r.get("筹码集中") is False else "—")
        cells = [r["code"], r["name"], r["price"], r["阶段"], chip, f"+{r['距高点%']:.0f}%", f"{r['抬底%']:.0f}%", r["量能比"], round(r["score_adj"], 1)]
        P.append("<tr>" + "".join(f"<td style='border-bottom:1px solid #eee'>{esc(x)}</td>" for x in cells) + "</tr>")
    P.append("</table>")
    P.append("<h2 style='margin-top:24px'>🔍 自选股每日体检</h2><table cellpadding=6 style='border-collapse:collapse;font-size:13px'><tr style='background:#f0f0f0'><th>代码</th><th>名称</th><th>状态</th><th>说明</th></tr>")
    for code, name, st, note in watch:
        color = "#0a0" if st not in ("未入选", "—") else "#999"
        P.append(f"<tr><td style='border-bottom:1px solid #eee'>{esc(code)}</td><td style='border-bottom:1px solid #eee'>{esc(name)}</td><td style='border-bottom:1px solid #eee;color:{color}'>{esc(st)}</td><td style='border-bottom:1px solid #eee'>{esc(note)}</td></tr>")
    P.append("</table><p style='color:#aaa;font-size:11px;margin-top:20px'>UZI 云端每日 · baostock+akshare · ③逼近前高优先 · 筹码集中加分。回测显示选股 alpha 薄，真实价值在大盘择时与筹码信号，仅供研究、非投资建议。</p>")
    return "<div style='font-family:sans-serif;max-width:860px'>" + "".join(P) + "</div>"


# 邮箱后缀 → (SMTP服务器, 端口, 是否SSL)。与 daily_stock_analysis 一致，从发件邮箱自动推。
DOMAIN_SMTP = {
    "qq.com": ("smtp.qq.com", 465, True), "foxmail.com": ("smtp.qq.com", 465, True),
    "163.com": ("smtp.163.com", 465, True), "126.com": ("smtp.126.com", 465, True),
    "139.com": ("smtp.139.com", 465, True), "sina.com": ("smtp.sina.com", 465, True),
    "sohu.com": ("smtp.sohu.com", 465, True), "aliyun.com": ("smtp.aliyun.com", 465, True),
    "gmail.com": ("smtp.gmail.com", 587, False), "outlook.com": ("smtp-mail.outlook.com", 587, False),
    "hotmail.com": ("smtp-mail.outlook.com", 587, False), "live.com": ("smtp-mail.outlook.com", 587, False),
}


def send_email(subject, html):
    import re
    from email.utils import formataddr
    # 兼容两套命名：EMAIL_*(daily_stock_analysis) 优先，SMTP_*/MAIL_* 兜底
    user = os.environ.get("EMAIL_SENDER") or os.environ.get("SMTP_USER")
    pw = os.environ.get("EMAIL_PASSWORD") or os.environ.get("SMTP_PASS")
    to_raw = os.environ.get("EMAIL_RECEIVERS") or os.environ.get("MAIL_TO") or ""
    to = [x.strip() for x in re.split(r"[,;\s]+", to_raw) if x.strip()]
    if not (user and pw and to):
        print("⚠️ 邮件环境变量不全(需 EMAIL_SENDER/EMAIL_PASSWORD/EMAIL_RECEIVERS)，跳过发信", file=sys.stderr)
        return
    domain = user.split("@")[-1].lower()
    host, port, use_ssl = DOMAIN_SMTP.get(domain, ("smtp." + domain, 465, True))
    host = os.environ.get("SMTP_HOST") or host                       # 允许显式覆盖
    port = int(os.environ.get("SMTP_PORT") or port)
    name = os.environ.get("EMAIL_SENDER_NAME", "UZI 每日选股")
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(name, "utf-8")), user))
    msg["To"] = ", ".join(to)
    ctx = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=ctx) as s:
            s.login(user, pw)
            s.sendmail(user, to, msg.as_string())
    else:
        with smtplib.SMTP(host, port) as s:
            s.starttls(context=ctx)
            s.login(user, pw)
            s.sendmail(user, to, msg.as_string())
    print(f"✅ 邮件已发送 → {', '.join(to)}（{host}:{port}）", file=sys.stderr)


def main():
    top_n = int(os.environ.get("TOP_N", "30"))
    print("[1/4] 载入中证500+1000 成分...", file=sys.stderr)
    uni = load_constituents()
    print(f"  {len(uni)} 只", file=sys.stderr)
    print("[2/4] baostock 抓月线...", file=sys.stderr)
    data = fetch_monthly(uni)
    print(f"  取到 {len(data)} 只月线", file=sys.stderr)
    print("[3/4] 大盘择时...", file=sys.stderr)
    regime = market_regime()
    print("[4/4] 筛选 + 筹码集中...", file=sys.stderr)
    hits = run_screen(data)
    watch = watch_status(load_watchlist(), data)
    html = build_html(hits, watch, regime, top_n)
    reg = "趋势市🟢" if (regime and regime["bull"]) else ("转弱🔴" if regime else "?")
    subject = f"UZI 每日选股 · {date.today()} · 命中{len(hits)} · 大盘{reg}"
    if os.environ.get("DRY_RUN") == "1":
        Path(__file__).parent.joinpath("last_report.html").write_text(html, encoding="utf-8")
        print(f"[DRY_RUN] 命中 {len(hits)} · 已写 last_report.html", file=sys.stderr)
    else:
        send_email(subject, html)


if __name__ == "__main__":
    main()
