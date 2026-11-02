#!/usr/bin/env python3
"""
analyze.py - trace_flows.py(또는 make_sample.py) 결과 폴더를 읽어
세탁 패턴 지표를 계산하고, 여러 사례를 나란히 비교하는 리포트를 만든다.

사용법:
  python3 analyze.py 이름=결과폴더 [이름=결과폴더 ...] [--out report]
  예) python3 analyze.py Bybit=out_bybit Binance=out_binance

출력:
  report/metrics.csv   사례별 지표 표
  report/report.html   비교 리포트 (차트 + 표, 브라우저로 열기)

지표 정의
  추적 유출액        시드(hop0)에서 나간 총액 (ETH 환산)
  출구 도달 비율     컨트랙트(브릿지/스왑 등)로 들어간 총액 / 추적 유출액
  첫 출구까지        첫 시드 유출 ~ 첫 컨트랙트 유입 (시간)
  T50 / T90          최종 출구 유입액의 50% / 90%가 도달하기까지 (시간)
  평균 팬아웃        유출이 있는 EOA 하나가 돈을 보낸 서로 다른 EOA 수 평균
  경유 지갑 비율     유출의 80% 이상을 다른 개인 지갑 한 곳으로 넘긴 EOA 비율 (필 체인/경유 지갑)
  최장 경유 체인     경유 지갑이 연속으로 이어진 최대 길이
  중앙 보유 시간     EOA가 처음 받은 뒤 처음 보내기까지 걸린 시간의 중앙값 (분)
  자산 구성          ETH / 스테이블코인 / BTC 래핑 / 기타 비중
"""
import argparse
import csv
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

ETH_LIKE = {"ETH", "ETH(internal)", "WETH", "stETH", "wstETH", "cbETH", "rETH"}
BTC_LIKE = {"WBTC", "cbBTC"}
STABLE = {"USDT", "USDC", "DAI"}


def eth_equiv(asset, amount):
    if asset in ETH_LIKE:
        return amount
    if asset in BTC_LIKE:
        return amount * 30
    return amount / 3000


def asset_group(asset):
    if asset in ETH_LIKE:
        return "ETH"
    if asset in STABLE:
        return "스테이블코인"
    if asset in BTC_LIKE:
        return "BTC 래핑"
    return "기타"


def parse_t(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def load(folder):
    nodes, edges = {}, []
    with open(os.path.join(folder, "nodes.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            nodes[r["address"]] = dict(hop=int(r["hop"]), type=r["type"],
                                       flags=r.get("flags", ""), label=r.get("label", ""))
    with open(os.path.join(folder, "edges.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = parse_t(r["time_utc"])
            if t is None:
                continue
            amt = float(r["amount"])
            edges.append(dict(src=r["from"], dst=r["to"], asset=r["asset"], amount=amt,
                              v=eth_equiv(r["asset"], amt), t=t, fn=r.get("function", "")))
    edges.sort(key=lambda e: e["t"])
    return nodes, edges


def is_synthetic(nodes):
    """make_sample.py가 만든 데이터는 라벨에 '(가상)'이 붙어 있다."""
    return any("(가상)" in (n.get("label") or "") for n in nodes.values())


def analyze(nodes, edges):
    seeds = {a for a, n in nodes.items() if n["hop"] == 0}
    is_contract = lambda a: nodes.get(a, {}).get("type") == "contract"
    is_eoa = lambda a: nodes.get(a, {}).get("type") == "EOA"

    seed_edges = [e for e in edges if e["src"] in seeds]
    if not seed_edges:
        return None
    t0 = seed_edges[0]["t"]
    seed_out = sum(e["v"] for e in seed_edges)

    exit_edges = [e for e in edges if is_contract(e["dst"])]
    exit_total = sum(e["v"] for e in exit_edges)

    # 누적 출구 도달 곡선 (시간 단위, 추적 유출액 대비 비율)
    curve, cum = [[0.0, 0.0]], 0.0
    for e in exit_edges:
        cum += e["v"]
        curve.append([round((e["t"] - t0) / 3600, 3), round(min(cum / seed_out, 1.0), 4)])

    def t_reach(frac):
        target, c = exit_total * frac, 0.0
        for e in exit_edges:
            c += e["v"]
            if c >= target:
                return (e["t"] - t0) / 3600
        return None

    # 노드 수 (hop별)
    max_hop = max(n["hop"] for n in nodes.values())
    per_hop = []
    for h in range(max_hop + 1):
        per_hop.append(dict(
            hop=h,
            eoa=sum(1 for n in nodes.values() if n["hop"] == h and n["type"] == "EOA"),
            contract=sum(1 for n in nodes.values() if n["hop"] == h and n["type"] == "contract"),
        ))

    # 유입/유출 집계
    out_by = defaultdict(lambda: defaultdict(float))
    in_total, out_total = defaultdict(float), defaultdict(float)
    first_in, first_out = {}, {}
    for e in edges:
        out_by[e["src"]][e["dst"]] += e["v"]
        out_total[e["src"]] += e["v"]
        in_total[e["dst"]] += e["v"]
        first_in.setdefault(e["dst"], e["t"])
        first_out.setdefault(e["src"], e["t"])

    fanouts = [sum(1 for d in out_by[a] if is_eoa(d))
               for a in nodes if is_eoa(a) and out_total[a] > 0]
    fanouts = [x for x in fanouts if x > 0]

    # 경유 지갑: hop>=1 EOA, 유출의 80% 이상이 한 곳으로
    passthru = {}
    for a, n in nodes.items():
        if n["hop"] >= 1 and n["type"] == "EOA" and out_total[a] > 0:
            dst, v = max(out_by[a].items(), key=lambda kv: kv[1])
            if v >= 0.8 * out_total[a] and is_eoa(dst):
                passthru[a] = dst
    candidates = [a for a, n in nodes.items() if n["hop"] >= 1 and n["type"] == "EOA" and out_total[a] > 0]

    def chain_len(a, seen):
        if a not in passthru or a in seen:
            return 0
        seen.add(a)
        return 1 + chain_len(passthru[a], seen)
    longest = max((chain_len(a, set()) for a in passthru), default=0)

    holds = [(first_out[a] - first_in[a]) / 60 for a in nodes
             if is_eoa(a) and nodes[a]["hop"] >= 1 and a in first_in and a in first_out
             and first_out[a] >= first_in[a]]

    mix = defaultdict(float)
    for e in edges:
        mix[asset_group(e["asset"])] += e["v"]
    mix_total = sum(mix.values()) or 1

    fn_val = defaultdict(float)
    for e in exit_edges:
        fn_val[e["fn"] or "(직접 송금)"] += e["v"]

    return dict(
        seed_count=len(seeds),
        node_count=len(nodes),
        edge_count=len(edges),
        seed_out=seed_out,
        exit_share=exit_total / seed_out if seed_out else 0,
        first_exit_h=(exit_edges[0]["t"] - t0) / 3600 if exit_edges else None,
        t50_h=t_reach(0.5) if exit_edges else None,
        t90_h=t_reach(0.9) if exit_edges else None,
        mean_fanout=statistics.mean(fanouts) if fanouts else 0,
        passthru_ratio=len(passthru) / len(candidates) if candidates else 0,
        longest_chain=longest,
        median_hold_min=statistics.median(holds) if holds else None,
        mix={k: v / mix_total for k, v in sorted(mix.items(), key=lambda kv: -kv[1])},
        top_exit_fns=sorted(fn_val.items(), key=lambda kv: -kv[1])[:5],
        per_hop=per_hop,
        curve=curve,
        start_utc=datetime.fromtimestamp(t0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
    )


ROWS = [
    ("seed_count", "시드 주소 수", "{:,.0f}"),
    ("node_count", "추적된 주소 수", "{:,.0f}"),
    ("edge_count", "송금 건수", "{:,.0f}"),
    ("seed_out", "추적 유출액 (ETH 환산)", "{:,.1f}"),
    ("exit_share", "출구 도달 비율", "{:.1%}"),
    ("first_exit_h", "첫 출구까지 (시간)", "{:.2f}"),
    ("t50_h", "T50 (시간)", "{:.2f}"),
    ("t90_h", "T90 (시간)", "{:.2f}"),
    ("mean_fanout", "평균 팬아웃", "{:.2f}"),
    ("passthru_ratio", "경유 지갑 비율", "{:.1%}"),
    ("longest_chain", "최장 경유 체인", "{:,.0f}"),
    ("median_hold_min", "중앙 보유 시간 (분)", "{:.1f}"),
]


def fmt(v, f):
    return "—" if v is None else f.format(v)


def main(argv=None):
    ap = argparse.ArgumentParser(description="세탁 패턴 지표 계산 및 사례 비교 리포트")
    ap.add_argument("cases", nargs="+", help="이름=결과폴더")
    ap.add_argument("--out", default="report")
    a = ap.parse_args(argv)

    results = []
    for c in a.cases:
        if "=" not in c:
            sys.exit(f"'이름=폴더' 형식이어야 합니다: {c}")
        name, folder = c.split("=", 1)
        if not os.path.exists(os.path.join(folder, "nodes.csv")):
            sys.exit(f"{folder}/nodes.csv 가 없습니다. make_sample.py 또는 trace_flows.py를 먼저 실행하세요.")
        nodes, edges = load(folder)
        m = analyze(nodes, edges)
        if m is None:
            print(f"! {name}: 시드 유출 기록이 없어 건너뜀", file=sys.stderr)
            continue
        m["name"] = name
        m["synthetic"] = is_synthetic(nodes)
        results.append(m)
    if not results:
        sys.exit("분석할 사례가 없습니다.")

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "metrics.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["지표"] + [r["name"] for r in results])
        for key, label, f_ in ROWS:
            w.writerow([label] + [fmt(r[key], f_) for r in results])
        groups = sorted({g for r in results for g in r["mix"]})
        for g in groups:
            w.writerow([f"자산 비중: {g}"] + [f"{r['mix'].get(g, 0):.1%}" for r in results])

    # 콘솔 요약
    print("\n" + " | ".join(["지표".ljust(22)] + [r["name"][:14].ljust(14) for r in results]))
    for key, label, f_ in ROWS:
        print(" | ".join([label.ljust(22)] + [fmt(r[key], f_).ljust(14) for r in results]))

    write_report(os.path.join(a.out, "report.html"), results)
    write_tistory(os.path.join(a.out, "report_tistory.html"), results)
    print(f"\n리포트: {os.path.abspath(os.path.join(a.out, 'report.html'))}")
    print(f"블로그용: {os.path.abspath(os.path.join(a.out, 'report_tistory.html'))}"
          "  (티스토리 HTML 모드에 붙여넣기)")


def write_report(path, results):
    table_rows = []
    for key, label, f_ in ROWS:
        table_rows.append([label] + [fmt(r[key], f_) for r in results])
    groups = sorted({g for r in results for g in r["mix"]})
    for g in groups:
        table_rows.append([f"자산 비중: {g}"] + [f"{r['mix'].get(g, 0):.1%}" for r in results])
    data = dict(
        cases=[dict(name=r["name"], curve=r["curve"], per_hop=r["per_hop"],
                    start=r["start_utc"], fns=r["top_exit_fns"]) for r in results],
        head=["지표"] + [r["name"] for r in results],
        rows=table_rows,
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(REPORT_HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False)))


# ---------------------------------------------------------------------------
# 블로그(티스토리)용 리포트
#   - <html>/<head>/<body> 없이 본문에 바로 붙이는 HTML 조각
#   - 스크립트 없이 정적 SVG로 차트를 그려, 블로그가 스크립트를 막아도 보인다
#   - 스타일을 모두 인라인으로 넣어 블로그 테마 CSS와 섞이지 않는다
# ---------------------------------------------------------------------------
T_COL = ["#2a78d6", "#eb6834", "#1baf7a"]
T_INK, T_INK2, T_MUTED, T_GRID, T_AXIS = "#0b0b0b", "#52514e", "#8a8984", "#e4e3df", "#b9b8b2"
T_FONT = ("-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic',"
          "'Noto Sans KR',sans-serif")


def _esc(x):
    return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _nice_max(v):
    import math
    if v <= 0:
        return 1
    p = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if m * p >= v:
            return m * p
    return 10 * p


def _num(v):
    return f"{v:g}" if v < 10 else f"{v:,.0f}"


def _legend(results):
    items = "".join(
        f'<span style="margin-right:16px;white-space:nowrap">'
        f'<span style="display:inline-block;width:12px;height:12px;border-radius:3px;'
        f'background:{T_COL[i % 3]};margin-right:6px;vertical-align:-1px"></span>{_esc(r["name"])}</span>'
        for i, r in enumerate(results))
    return f'<p style="margin:6px 0 4px;font-size:13px;color:{T_INK2}">{items}</p>'


def _svg_curve(results):
    W, H = 520, 280
    l, r_, t, b = 42, 62, 14, 42
    xmax = _nice_max(max(r["curve"][-1][0] for r in results) * 1.02)
    X = lambda v: l + v / xmax * (W - l - r_)
    Y = lambda v: H - b - v * (H - t - b)
    o = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="누적 출구 도달 비율" '
         f'style="display:block;max-width:100%;height:auto;font-family:{T_FONT}">']
    for i in range(5):
        v = i / 4
        o.append(f'<line x1="{l}" x2="{W - r_}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="{T_GRID}"/>')
        o.append(f'<text x="{l - 8}" y="{Y(v) + 4:.1f}" text-anchor="end" font-size="12.5" '
                 f'fill="{T_INK2}">{int(v * 100)}%</text>')
    for i in range(6):
        v = xmax * i / 5
        o.append(f'<text x="{X(v):.1f}" y="{H - b + 18}" text-anchor="middle" font-size="12.5" '
                 f'fill="{T_INK2}">{_num(v)}h</text>')
    o.append(f'<line x1="{l}" x2="{W - r_}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" stroke="{T_AXIS}"/>')
    o.append(f'<text x="{W - r_}" y="{H - 4}" text-anchor="end" font-size="12.5" fill="{T_MUTED}">'
             f'시드 첫 유출 이후 경과 시간</text>')
    ends = []
    for i, r in enumerate(results):
        d, first = "", True
        for tt, vv in r["curve"]:
            d += f"M{X(tt):.1f},{Y(vv):.1f}" if first else f"H{X(tt):.1f}V{Y(vv):.1f}"
            first = False
        d += f"H{X(xmax):.1f}"
        o.append(f'<path d="{d}" fill="none" stroke="{T_COL[i % 3]}" stroke-width="2" '
                 f'stroke-linejoin="round"/>')
        ends.append([Y(r["curve"][-1][1]) + 4, i])
    ends.sort()
    for k in range(1, len(ends)):          # 끝 라벨 겹침 방지
        if ends[k][0] - ends[k - 1][0] < 14:
            ends[k][0] = ends[k - 1][0] + 14
    for y, i in ends:
        o.append(f'<text x="{W - r_ + 6}" y="{y:.1f}" font-size="13" fill="{T_INK}">'
                 f'{_esc(results[i]["name"])}</text>')
    o.append("</svg>")
    return "".join(o)


def _svg_bars(results):
    W, H = 520, 250
    l, r_, t, b = 38, 6, 16, 42
    hops = max(len(r["per_hop"]) for r in results)
    ymax = _nice_max(max(h["eoa"] for r in results for h in r["per_hop"]))
    Y = lambda v: H - b - v / ymax * (H - t - b)
    n = len(results)
    gw = (W - l - r_) / hops
    bw = min(34, (gw * 0.72 - 2 * (n - 1)) / n)
    step = max(1, -(-34 // int(gw))) if gw < 34 else 1
    o = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="hop별 개인 지갑 수" '
         f'style="display:block;max-width:100%;height:auto;font-family:{T_FONT}">']
    yt = 4 if ymax % 4 == 0 else 5
    for i in range(yt + 1):
        v = ymax * i / yt
        o.append(f'<line x1="{l}" x2="{W - r_}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="{T_GRID}"/>')
        o.append(f'<text x="{l - 8}" y="{Y(v) + 4:.1f}" text-anchor="end" font-size="12.5" '
                 f'fill="{T_INK2}">{round(v)}</text>')
    for h in range(hops):
        gx = l + gw * h + gw / 2
        x0 = gx - (n * bw + 2 * (n - 1)) / 2
        if h % step == 0:
            lab = f"hop {h}" if gw >= 44 else str(h)
            o.append(f'<text x="{gx:.1f}" y="{H - b + 18}" text-anchor="middle" font-size="12.5" '
                     f'fill="{T_INK2}">{lab}</text>')
        for i, r in enumerate(results):
            v = r["per_hop"][h]["eoa"] if h < len(r["per_hop"]) else 0
            if v <= 0:
                continue
            x, y = x0 + i * (bw + 2), Y(v)
            hgt = H - b - y
            rr = min(4, hgt, bw / 2)
            o.append(f'<path d="M{x:.1f},{H - b}V{y + rr:.1f}Q{x:.1f},{y:.1f} {x + rr:.1f},{y:.1f}'
                     f'H{x + bw - rr:.1f}Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + rr:.1f}V{H - b}Z" '
                     f'fill="{T_COL[i % 3]}"><title>{_esc(r["name"])} · hop {h}: 지갑 {v}개</title></path>')
    o.append(f'<line x1="{l}" x2="{W - r_}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" stroke="{T_AXIS}"/>')
    o.append(f'<text x="{W - r_}" y="{H - 4}" text-anchor="end" font-size="12.5" fill="{T_MUTED}">'
             f'시드에서 몇 단계 떨어졌나 (hop)</text>')
    o.append("</svg>")
    return "".join(o)


def _speed_summary(results):
    lines = []
    for r in results:
        if r["t50_h"] is None:
            lines.append(f"<b>{_esc(r['name'])}</b>: 출구(컨트랙트)로 들어간 기록이 없다.")
            continue
        lines.append(
            f"<b>{_esc(r['name'])}</b>: 첫 출구까지 {r['first_exit_h']:.1f}시간, "
            f"출구 유입액의 절반(T50)은 {r['t50_h']:.1f}시간, 90%(T90)는 {r['t90_h']:.1f}시간 만에 도달했다. "
            f"추적한 유출액 중 {r['exit_share']:.0%}가 출구를 지났다.")
    timed = [r for r in results if r["t90_h"]]
    if len(timed) >= 2:
        fast = min(timed, key=lambda r: r["t90_h"])
        slow = max(timed, key=lambda r: r["t90_h"])
        if fast is not slow and fast["t90_h"] > 0:
            lines.append(f"T90 기준으로 {_esc(fast['name'])}가 {_esc(slow['name'])}보다 "
                         f"약 {slow['t90_h'] / fast['t90_h']:.0f}배 빨리 빠져나갔다.")
    return lines


def _spread_summary(results):
    lines = []
    for r in results:
        peak = max(r["per_hop"], key=lambda h: h["eoa"])
        lines.append(
            f"<b>{_esc(r['name'])}</b>: 지갑이 가장 많은 단계는 hop {peak['hop']}({peak['eoa']}개)이고, "
            f"지갑 하나가 평균 {r['mean_fanout']:.1f}곳으로 돈을 나눴다. "
            f"한 곳으로만 넘기는 경유 지갑이 {r['passthru_ratio']:.0%}, 최장 경유 체인은 {r['longest_chain']}단계다.")
    return lines


LI = f'style="margin:0 0 4px;font-size:14px;line-height:1.7;color:{T_INK}"'

GLOSSARY = [
    ("출구 도달 비율", "추적한 유출액 중 브릿지·스왑 등 컨트랙트로 들어간 비율. 높을수록 이미 빠져나간 돈이 많다."),
    ("T50 / T90", "출구로 들어간 돈의 50% / 90%가 도달하기까지 걸린 시간. 세탁 속도를 보여 준다."),
    ("평균 팬아웃", "개인 지갑 하나가 돈을 나눠 보낸 지갑 수의 평균. 클수록 넓게 흩뿌리는 분산형이다."),
    ("경유 지갑 비율", "받은 돈의 80% 이상을 다른 지갑 한 곳으로 넘긴 지갑의 비율. 높으면 필 체인형이다."),
    ("최장 경유 체인", "경유 지갑이 줄줄이 이어진 최대 길이."),
    ("중앙 보유 시간", "지갑이 돈을 받은 뒤 다시 보내기까지 걸린 시간의 중앙값. 짧을수록 자동화 가능성이 크다."),
]


def write_tistory(path, results):
    p = f'style="margin:0 0 10px;font-size:15px;line-height:1.7;color:{T_INK}"'
    h = f'style="margin:28px 0 6px;font-size:18px;font-weight:700;color:{T_INK}"'
    sub = f'style="margin:0 0 8px;font-size:14px;line-height:1.6;color:{T_INK2}"'
    ul = f'style="margin:8px 0 0;padding-left:20px;font-size:14px;line-height:1.7;color:{T_INK}"'
    box = (f'style="margin:0 0 18px;padding:12px 14px;border-radius:8px;background:#fdf3e7;'
           f'border:1px solid #f1d3a8;font-size:14px;line-height:1.6;color:{T_INK}"')
    o = [f'<div style="max-width:720px;margin:0 auto;padding:22px 20px;background:#ffffff;'
         f'border:1px solid {T_GRID};border-radius:12px;font-family:{T_FONT};color:{T_INK};'
         f'box-sizing:border-box">']
    o.append(f'<p style="margin:0 0 4px;font-size:21px;font-weight:700;color:{T_INK}">세탁 패턴 비교 리포트</p>')
    o.append(f'<p {sub}>' + " · ".join(f"{_esc(r['name'])}: {r['start_utc']} UTC 시작" for r in results) + "</p>")
    synth = [r["name"] for r in results if r.get("synthetic")]
    if synth:
        o.append(f'<p {box}><b>참고</b>: {_esc(", ".join(synth))}는 실습용 스크립트(make_sample.py)가 '
                 f'무작위로 만든 <b>합성 데이터</b>다. 주소·금액·시각은 모두 가짜이며 실제 사건이 아니다. '
                 f'실제 세탁에서 관찰되는 두 패턴(빠른 분산형, 필 체인형)을 단순화해 흉내 냈다.</p>')
    o.append(f'<p {p}>공개 블록체인 기록에서 시드(공격자) 지갑을 출발점으로 자금 흐름을 따라가, '
             f'돈이 얼마나 빨리, 얼마나 넓게 퍼져 브릿지·스왑 같은 출구로 빠져나가는지를 지표로 비교했다.</p>')

    o.append(f'<p {h}>1. 출구 도달 속도</p>')
    o.append(f'<p {sub}>가로축은 시드에서 처음 돈이 나간 뒤 흐른 시간, 세로축은 그때까지 출구(컨트랙트)에 '
             f'도달한 금액의 누적 비율이다. 선이 빨리 오를수록 세탁이 빠르고, 동결할 시간이 짧다.</p>')
    o.append(_legend(results))
    o.append(_svg_curve(results))
    o.append(f'<ul {ul}>' + "".join(f"<li {LI}>{x}</li>" for x in _speed_summary(results)) + "</ul>")

    o.append(f'<p {h}>2. 단계별 지갑 수 (분산 정도)</p>')
    o.append(f'<p {sub}>hop은 시드에서 몇 번 송금을 거쳐 도달했는지를 뜻한다. 각 단계에서 돈을 받은 개인 지갑 수가 '
             f'빨리 늘면 수많은 지갑으로 흩뿌리는 분산형이고, 단계마다 1~2개로 길게 이어지면 필 체인형이다.</p>')
    o.append(_legend(results))
    o.append(_svg_bars(results))
    o.append(f'<ul {ul}>' + "".join(f"<li {LI}>{x}</li>" for x in _spread_summary(results)) + "</ul>")

    o.append(f'<p {h}>3. 지표 표</p>')
    cell = "padding:8px;border:0;background:none;font-size:14px;line-height:1.5;vertical-align:middle;"
    th = f'style="{cell}border-bottom:2px solid {T_INK};text-align:right;font-weight:700;white-space:nowrap;color:{T_INK}"'
    th0 = f'style="{cell}border-bottom:2px solid {T_INK};text-align:left;font-weight:700;color:{T_INK}"'
    td = f'style="{cell}border-bottom:1px solid {T_GRID};text-align:right;white-space:nowrap;color:{T_INK}"'
    td0 = f'style="{cell}border-bottom:1px solid {T_GRID};text-align:left;color:{T_INK2}"'
    rows = [[label] + [fmt(r[key], f_) for r in results] for key, label, f_ in ROWS]
    for g in sorted({g for r in results for g in r["mix"]}):
        rows.append([f"자산 비중: {g}"] + [f"{r['mix'].get(g, 0):.1%}" for r in results])
    o.append('<div style="overflow-x:auto"><table style="width:100%;margin:0;border:0;border-collapse:collapse;font-size:14px;'
             f'color:{T_INK}"><thead><tr><th {th0}>지표</th>'
             + "".join(f"<th {th}>{_esc(r['name'])}</th>" for r in results) + "</tr></thead><tbody>"
             + "".join(f"<tr><td {td0}>{_esc(row[0])}</td>" + "".join(f"<td {td}>{_esc(c)}</td>" for c in row[1:])
                       + "</tr>" for row in rows)
             + "</tbody></table></div>")

    o.append(f'<p {h}>지표 읽는 법</p>')
    o.append(f'<ul {ul}>' + "".join(f"<li {LI}><b>{k}</b>: {v}</li>" for k, v in GLOSSARY) + "</ul>")
    o.append(f'<p style="margin:18px 0 0;font-size:12.5px;line-height:1.6;color:{T_MUTED}">'
             f'ETH 환산은 비교용 근사치다(스테이블코인 3,000달러≈1 ETH, BTC 래핑 1개≈30 ETH). '
             f'공개 데이터에서 얻은 단서이며 사실을 확정하는 자료가 아니다.</p>')
    o.append("</div>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("<!-- 티스토리: 글쓰기 > 우측 상단 '기본모드'를 'HTML'로 바꾼 뒤 이 내용을 통째로 붙여넣기 -->\n")
        f.write("".join(o))


REPORT_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>세탁 패턴 비교</title>
<style>
:root{color-scheme:light;
 --surface:#fcfcfb;--panel:#f3f2ef;--text-1:#0b0b0b;--text-2:#52514e;--muted:#8a8984;
 --grid:#e4e3df;--axis:#b9b8b2;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;
 --surface:#1a1a19;--panel:#242422;--text-1:#f0efec;--text-2:#c3c2b7;--muted:#8f8e88;
 --grid:#33332f;--axis:#55544f;--s1:#3987e5;--s2:#d95926;--s3:#199e70;}}
:root[data-theme="dark"]{color-scheme:dark;
 --surface:#1a1a19;--panel:#242422;--text-1:#f0efec;--text-2:#c3c2b7;--muted:#8f8e88;
 --grid:#33332f;--axis:#55544f;--s1:#3987e5;--s2:#d95926;--s3:#199e70;}
*{box-sizing:border-box}
body{margin:0;background:var(--surface);color:var(--text-1);
 font:15px/1.55 system-ui,-apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}
main{max-width:920px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 4px}
.sub{color:var(--text-2);margin:0 0 8px;font-size:14px}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--text-2);margin:8px 0}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.chart{position:relative;width:100%}
svg{display:block;width:100%;height:auto;overflow:visible}
.tip{position:absolute;pointer-events:none;background:var(--panel);color:var(--text-1);
 border:1px solid var(--grid);border-radius:8px;padding:6px 10px;font-size:12px;
 white-space:nowrap;opacity:0;transition:opacity .08s;box-shadow:0 2px 8px #0002}
table{width:100%;border-collapse:collapse;font-size:14px;margin-top:8px}
th,td{padding:7px 8px;border-bottom:1px solid var(--grid);text-align:right}
th:first-child,td:first-child{text-align:left;color:var(--text-2)}
th{font-weight:600}
.wrap{overflow-x:auto}
.note{font-size:13px;color:var(--muted);margin-top:24px}
ul.fns{margin:4px 0 0;padding-left:18px;font-size:14px;color:var(--text-2)}
</style></head>
<body><main>
<h1>세탁 패턴 비교 리포트</h1>
<p class="sub" id="meta"></p>

<h2>출구 도달 속도</h2>
<p class="sub">시드 첫 유출 이후 시간에 따라, 추적 유출액 중 브릿지·스왑 등 컨트랙트에 도달한 누적 비율</p>
<div class="legend" id="lg1"></div>
<div class="chart" id="c1"><div class="tip"></div></div>

<h2>hop별 주소 수 (분산 정도)</h2>
<p class="sub">각 단계에서 자금을 받은 개인 지갑(EOA) 수. 막대가 빨리 커질수록 넓게 흩뿌리는 전략</p>
<div class="legend" id="lg2"></div>
<div class="chart" id="c2"><div class="tip"></div></div>

<h2>지표 표</h2>
<div class="wrap"><table id="tbl"></table></div>

<h2>주요 출구 함수</h2>
<div id="fns"></div>

<p class="note">ETH 환산은 정렬·비교용 근사치(스테이블코인 1 ETH≈3,000달러, BTC 래핑 1≈30 ETH)입니다. 공개 데이터 기반 단서이며 사실 확정이 아닙니다.</p>
</main>
<script>
const D=__DATA__;
const COLORS=['var(--s1)','var(--s2)','var(--s3)'];
const NS='http://www.w3.org/2000/svg';
const el=(t,a={},p)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);if(p)p.appendChild(e);return e;};
document.getElementById('meta').textContent=D.cases.map(c=>`${c.name}: ${c.start} UTC 시작`).join(' · ');
function legend(id){const L=document.getElementById(id);D.cases.forEach((c,i)=>{const s=document.createElement('span');s.innerHTML=`<i style="background:${COLORS[i%3]}"></i>`;s.append(c.name);L.appendChild(s);});}
legend('lg1');legend('lg2');
function niceMax(v){if(v<=0)return 1;const p=Math.pow(10,Math.floor(Math.log10(v)));for(const m of[1,2,2.5,5,10])if(m*p>=v)return m*p;return 10*p;}

// 차트 1: 누적 출구 도달 곡선 (계단형)
(function(){
 const box=document.getElementById('c1'),tip=box.querySelector('.tip');
 const W=Math.max(320,box.clientWidth),H=W<500?240:300,m={l:44,r:64,t:12,b:36};
 const xmax=niceMax(Math.max(...D.cases.map(c=>c.curve[c.curve.length-1][0]))*1.02);
 const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':'누적 출구 도달 비율'});box.prepend(svg);
 const X=v=>m.l+v/xmax*(W-m.l-m.r),Y=v=>H-m.b-v*(H-m.t-m.b);
 for(let i=0;i<=4;i++){const v=i/4;el('line',{x1:m.l,x2:W-m.r,y1:Y(v),y2:Y(v),stroke:'var(--grid)'},svg);
  el('text',{x:m.l-8,y:Y(v)+4,'text-anchor':'end','font-size':11,fill:'var(--text-2)'},svg).textContent=(v*100)+'%';}
 const xt=W<500?4:5;for(let i=0;i<=xt;i++){const v=xmax*i/xt;el('text',{x:X(v),y:H-m.b+18,'text-anchor':'middle','font-size':11,fill:'var(--text-2)'},svg).textContent=(+v.toFixed(1))+'h';}
 el('line',{x1:m.l,x2:W-m.r,y1:Y(0),y2:Y(0),stroke:'var(--axis)'},svg);
 el('text',{x:W-m.r,y:H-4,'text-anchor':'end','font-size':11,fill:'var(--muted)'},svg).textContent='시드 첫 유출 이후 경과 시간';
 const step=c=>{let d='',prev=null;for(const[t,v]of c.curve){d+=prev===null?`M${X(t)},${Y(v)}`:`H${X(t)}V${Y(v)}`;prev=v;}return d+`H${X(xmax)}`;};
 D.cases.forEach((c,i)=>{el('path',{d:step(c),fill:'none',stroke:COLORS[i%3],'stroke-width':2,'stroke-linejoin':'round'},svg);
  const last=c.curve[c.curve.length-1][1];
  el('text',{x:W-m.r+6,y:Y(last)+4+(i?12:0)*(Math.abs(last-(D.cases[0].curve.slice(-1)[0][1]))<.06?1:0),'font-size':12,fill:'var(--text-1)'},svg).textContent=c.name;});
 const cross=el('line',{y1:m.t,y2:H-m.b,stroke:'var(--axis)','stroke-dasharray':'3 3',opacity:0},svg);
 const dots=D.cases.map((c,i)=>el('circle',{r:4,fill:COLORS[i%3],stroke:'var(--surface)','stroke-width':2,opacity:0},svg));
 const valAt=(c,t)=>{let v=0;for(const[tt,vv]of c.curve){if(tt<=t)v=vv;else break;}return v;};
 const hit=el('rect',{x:m.l,y:m.t,width:W-m.l-m.r,height:H-m.t-m.b,fill:'transparent'},svg);
 const move=ev=>{const r=svg.getBoundingClientRect(),px=(ev.clientX-r.left)/r.width*W;
  const t=Math.max(0,Math.min(xmax,(px-m.l)/(W-m.l-m.r)*xmax));cross.setAttribute('x1',X(t));cross.setAttribute('x2',X(t));cross.setAttribute('opacity',1);
  let html=`<b>${t.toFixed(2)}시간</b>`;D.cases.forEach((c,i)=>{const v=valAt(c,t);dots[i].setAttribute('cx',X(t));dots[i].setAttribute('cy',Y(v));dots[i].setAttribute('opacity',1);
   html+=`<br><span style="color:${COLORS[i%3]}">●</span> ${c.name}: ${(v*100).toFixed(1)}%`;});
  tip.innerHTML=html;tip.style.opacity=1;const bx=box.getBoundingClientRect();let lx=ev.clientX-bx.left+12;if(lx>bx.width-170)lx-=190;tip.style.left=lx+'px';tip.style.top='8px';};
 hit.addEventListener('pointermove',move);hit.addEventListener('pointerleave',()=>{tip.style.opacity=0;cross.setAttribute('opacity',0);dots.forEach(d=>d.setAttribute('opacity',0));});
})();

// 차트 2: hop별 EOA 수 (그룹 막대)
(function(){
 const box=document.getElementById('c2'),tip=box.querySelector('.tip');
 const hops=Math.max(...D.cases.map(c=>c.per_hop.length));
 const W=Math.max(320,box.clientWidth),H=W<500?220:260,m={l:40,r:8,t:20,b:36};
 const ymax=niceMax(Math.max(...D.cases.flatMap(c=>c.per_hop.map(h=>h.eoa))));
 const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':'hop별 EOA 수'});box.prepend(svg);
 const Y=v=>H-m.b-v/ymax*(H-m.t-m.b);
 const yt=ymax%4===0?4:5;for(let i=0;i<=yt;i++){const v=ymax*i/yt;el('line',{x1:m.l,x2:W-m.r,y1:Y(v),y2:Y(v),stroke:'var(--grid)'},svg);
  el('text',{x:m.l-8,y:Y(v)+4,'text-anchor':'end','font-size':11,fill:'var(--text-2)'},svg).textContent=Math.round(v);}
 const gw=(W-m.l-m.r)/hops,n=D.cases.length,bw=Math.min(36,(gw*0.7-2*(n-1))/n);
 for(let h=0;h<hops;h++){
  const gx=m.l+gw*h+gw/2,start=gx-(n*bw+2*(n-1))/2;
  if(gw>=34||h%Math.ceil(34/gw)===0)el('text',{x:gx,y:H-m.b+18,'text-anchor':'middle','font-size':11,fill:'var(--text-2)'},svg).textContent=gw>=44?'hop '+h:''+h;
  D.cases.forEach((c,i)=>{const v=(c.per_hop[h]||{eoa:0}).eoa,x=start+i*(bw+2),y=Y(v),hgt=H-m.b-y;
   if(hgt>0){const r=Math.min(4,hgt,bw/2);
    el('path',{d:`M${x},${H-m.b}V${y+r}Q${x},${y} ${x+r},${y}H${x+bw-r}Q${x+bw},${y} ${x+bw},${y+r}V${H-m.b}Z`,fill:COLORS[i%3]},svg);}
   const hit=el('rect',{x:x-1,y:m.t,width:bw+2,height:H-m.t-m.b,fill:'transparent'},svg);
   hit.addEventListener('pointermove',ev=>{const bx=box.getBoundingClientRect();tip.innerHTML=`<b>${c.name} · hop ${h}</b><br>EOA ${v}개 · 컨트랙트 ${(c.per_hop[h]||{contract:0}).contract}개`;
    tip.style.opacity=1;let lx=ev.clientX-bx.left+12;if(lx>bx.width-180)lx-=200;tip.style.left=lx+'px';tip.style.top='8px';});
   hit.addEventListener('pointerleave',()=>tip.style.opacity=0);});
 }
 el('line',{x1:m.l,x2:W-m.r,y1:Y(0),y2:Y(0),stroke:'var(--axis)'},svg);
})();

// 표
(function(){const T=document.getElementById('tbl');
 T.innerHTML='<thead><tr>'+D.head.map(h=>`<th>${h}</th>`).join('')+'</tr></thead><tbody>'+
 D.rows.map(r=>'<tr>'+r.map(c=>`<td>${c}</td>`).join('')+'</tr>').join('')+'</tbody>';})();
(function(){const F=document.getElementById('fns');
 F.innerHTML=D.cases.map((c,i)=>`<p style="margin:12px 0 0"><span style="color:${COLORS[i%3]}">●</span> <b>${c.name}</b></p><ul class="fns">`+
  (c.fns.length?c.fns.map(([f,v])=>`<li>${f} — 약 ${v.toLocaleString(undefined,{maximumFractionDigits:1})} ETH 환산</li>`).join(''):'<li>출구 유입 없음</li>')+'</ul>').join('');})();
</script></body></html>
"""

if __name__ == "__main__":
    main()
