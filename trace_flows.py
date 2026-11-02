#!/usr/bin/env python3
"""
trace_flows.py - 공개된 시드 주소(예: FBI 공개 목록)에서 출발해
이더리움 자금 흐름을 N-hop까지 추적하고, 출구(브릿지/스왑/거래소 등
컨트랙트)로 보이는 지점을 표시한다.

데이터 출처: Etherscan API V2 (무료 키: https://etherscan.io/myapikey)

사용법:
  export ETHERSCAN_API_KEY=키
  python3 trace_flows.py addresses.txt --since-block 23690000 --hops 3

  Colab: 왼쪽 열쇠(🔑) 아이콘 > 보안 비밀에 ETHERSCAN_API_KEY 를 등록하면
         키를 따로 넘기지 않아도 자동으로 읽는다.
         !python trace_flows.py addresses.txt --hops 2

입력:
  addresses.txt   한 줄에 주소 하나 (# 주석 가능)
  --labels FILE   (선택) address,label CSV. 알려진 거래소/브릿지 주소 등

출력 (out/ 폴더):
  nodes.csv       주소, hop, EOA/컨트랙트, 라벨, 유입/유출 합계
  edges.csv       송금 내역 (from, to, 자산, 수량, tx, 시각, 호출 함수)
  exits.csv       자금이 흘러든 컨트랙트(=출구 후보) 유입액 순위
  graph.html      브라우저로 여는 인터랙티브 흐름 그래프

주의: 공개 블록체인 데이터 조회만 수행한다. 이 주소들로 송금하거나
상호작용하지 말 것. 결과는 단서일 뿐이며, 의미 있는 발견은
tips.fbi.gov / ic3.gov 에 근거(tx 해시)와 함께 제보할 것.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone

API = "https://api.etherscan.io/v2/api"
CHAIN_ID = 1  # Ethereum mainnet

# 추적할 토큰과 최소 수량 (이보다 작은 이동은 무시)
TRACK_TOKENS = {
    "WETH": 1.0, "stETH": 1.0, "wstETH": 1.0, "cbETH": 1.0, "rETH": 1.0,
    "USDT": 10_000, "USDC": 10_000, "DAI": 10_000,
    "WBTC": 0.05, "cbBTC": 0.05,
}

# 호출 함수명에 이런 단어가 있으면 브릿지/스왑/믹서 출구 후보로 표시
EXIT_HINTS = [
    "bridge", "swap", "transferout", "outbound", "deposit", "relay",
    "send", "lock", "exactinput", "multicall", "execute", "fill",
]


class Etherscan:
    def __init__(self, key, rps=3.0):
        self.key = key
        self.min_gap = 1.0 / rps
        self.last = 0.0
        self.calls = 0
        self.ok = 0          # 성공한 호출 수 (키가 유효한지 판단에 사용)

    def get(self, **params):
        params.update(chainid=CHAIN_ID, apikey=self.key)
        url = API + "?" + urllib.parse.urlencode(params)
        for attempt in range(6):
            wait = self.min_gap - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.calls += 1
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    data = json.loads(r.read().decode())
            except Exception as e:
                print(f"  ! 요청 오류({e}), 재시도", file=sys.stderr)
                time.sleep(2 * (attempt + 1))
                continue
            res = data.get("result")
            if isinstance(res, str) and "rate limit" in res.lower():
                time.sleep(1.5 * (attempt + 1))
                continue
            if data.get("status") == "0":
                msg = str(data.get("message", "")) + str(res)
                if "No transactions found" in msg or "No records found" in msg:
                    self.ok += 1
                    return []
                if ("Invalid API Key" in msg or "Missing" in msg) and self.ok > 0:
                    # 앞서 성공한 키가 갑자기 거부되면 대개 일시적 제한이다. 쉬었다가 재시도
                    print(f"  ! 일시적 키 거부({msg[:40]}), {5 * (attempt + 1)}초 후 재시도",
                          file=sys.stderr)
                    time.sleep(5 * (attempt + 1))
                    continue
                if "Invalid API Key" in msg or "Missing" in msg:
                    sys.exit(f"API 키 오류: {msg}\n"
                             "  - etherscan.io/myapikey 에서 만든 키인지 확인하세요 (BscScan 등 다른 사이트 키는 안 됩니다).\n"
                             "  - 보안 비밀 값에 공백·따옴표·줄바꿈 없이 키만 들어갔는지 확인하세요.\n"
                             "  - 방금 만든 키라면 몇 분 뒤 다시 시도하세요.")
                print(f"  ! API 응답: {msg[:120]}", file=sys.stderr)
                return []
            self.ok += 1
            return res
        print("  ! 재시도 한도 초과, 건너뜀", file=sys.stderr)
        return []

    def txlist(self, addr, start):
        return self.get(module="account", action="txlist", address=addr,
                        startblock=start, endblock=99999999,
                        page=1, offset=1000, sort="asc")

    def internal(self, addr, start):
        return self.get(module="account", action="txlistinternal", address=addr,
                        startblock=start, endblock=99999999,
                        page=1, offset=1000, sort="asc")

    def tokentx(self, addr, start):
        return self.get(module="account", action="tokentx", address=addr,
                        startblock=start, endblock=99999999,
                        page=1, offset=1000, sort="asc")

    def is_contract(self, addr):
        res = self.get(module="proxy", action="eth_getCode",
                       address=addr, tag="latest")
        return isinstance(res, str) and res not in ("0x", "0x0", "")


def get_api_key(cli_key=None):
    """--api-key > 환경변수 > Colab 보안 비밀 순으로 API 키를 찾는다."""
    clean = lambda k: (k or "").strip().strip("'\"").strip() or None  # 복사할 때 섞인 공백·따옴표 제거
    if cli_key:
        return clean(cli_key)
    if os.environ.get("ETHERSCAN_API_KEY"):
        return clean(os.environ["ETHERSCAN_API_KEY"])
    try:
        from google.colab import userdata  # Colab에서만 존재
        return clean(userdata.get("ETHERSCAN_API_KEY"))
    except Exception:
        return None


def ts(t):
    try:
        return datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def fn_name(tx):
    f = (tx.get("functionName") or "").split("(")[0]
    return f


def outgoing(es, addr, start, min_eth):
    """addr에서 나간 의미 있는 이동 목록"""
    edges = []
    for tx in es.txlist(addr, start):
        if tx.get("from", "").lower() != addr or tx.get("isError") == "1":
            continue
        to = (tx.get("to") or "").lower()
        if not to:
            continue
        val = int(tx.get("value", "0")) / 1e18
        f = fn_name(tx)
        # ETH 이동이거나, 값이 없어도 컨트랙트 호출(토큰 승인/브릿지 호출) 기록
        if val >= min_eth or (val > 0 and f):
            edges.append(dict(src=addr, dst=to, asset="ETH", amount=val,
                              tx=tx["hash"], time=ts(tx.get("timeStamp")), fn=f))
    for tx in es.internal(addr, start):
        if tx.get("from", "").lower() != addr or tx.get("isError") == "1":
            continue
        to = (tx.get("to") or "").lower()
        val = int(tx.get("value", "0")) / 1e18
        if to and val >= min_eth:
            edges.append(dict(src=addr, dst=to, asset="ETH(internal)", amount=val,
                              tx=tx["hash"], time=ts(tx.get("timeStamp")), fn=""))
    for tx in es.tokentx(addr, start):
        if tx.get("from", "").lower() != addr:
            continue
        sym = tx.get("tokenSymbol", "")
        if sym not in TRACK_TOKENS:
            continue
        dec = int(tx.get("tokenDecimal") or 18)
        amt = int(tx.get("value", "0")) / (10 ** dec)
        if amt >= TRACK_TOKENS[sym]:
            edges.append(dict(src=addr, dst=tx["to"].lower(), asset=sym, amount=amt,
                              tx=tx["hash"], time=ts(tx.get("timeStamp")), fn=""))
    return edges


def eth_equiv(e):
    """팬아웃 정렬용 대략적 크기 (ETH 계열만 비교, 나머지는 보조)"""
    if e["asset"].startswith("ETH") or e["asset"] in ("WETH", "stETH", "wstETH", "cbETH", "rETH"):
        return e["amount"]
    if e["asset"] in ("WBTC", "cbBTC"):
        return e["amount"] * 30
    return e["amount"] / 3000  # 스테이블코인 대략 환산


def load_list(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip().strip(",")
            if line.lower().startswith("0x") and len(line) == 42:
                out.append(line.lower())
    return list(dict.fromkeys(out))


def load_labels(path):
    labels = {}
    if not path:
        return labels
    with open(path, encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) >= 2 and row[0].lower().startswith("0x"):
                labels[row[0].strip().lower()] = row[1].strip()
    return labels


def main(argv=None):
    ap = argparse.ArgumentParser(description="공개 시드 주소 기반 ETH 자금 흐름 추적")
    ap.add_argument("addresses", help="시드 주소 파일 (한 줄에 하나)")
    ap.add_argument("--api-key", default=None, help="생략 시 환경변수/Colab 보안 비밀 사용")
    ap.add_argument("--since-block", type=int, default=0,
                    help="이 블록 이후 거래만 조회 (해킹 발생 블록 권장)")
    ap.add_argument("--hops", type=int, default=3, help="추적 깊이")
    ap.add_argument("--min-eth", type=float, default=1.0, help="최소 ETH 이동량")
    ap.add_argument("--max-children", type=int, default=15,
                    help="노드당 따라갈 최대 수신 주소 수 (큰 금액 순)")
    ap.add_argument("--max-nodes", type=int, default=600,
                    help="시드 외에 새로 추적할 최대 주소 수")
    ap.add_argument("--labels", help="address,label CSV (선택)")
    ap.add_argument("--rps", type=float, default=3.0, help="초당 API 호출 (무료 키는 3 권장)")
    ap.add_argument("--out", default="out")
    a = ap.parse_args(argv)

    a.api_key = get_api_key(a.api_key)
    if not a.api_key:
        sys.exit("API 키가 없습니다. --api-key, 환경변수 ETHERSCAN_API_KEY, "
                 "또는 Colab 보안 비밀(ETHERSCAN_API_KEY) 중 하나로 넘겨 주세요.")

    es = Etherscan(a.api_key, a.rps)
    seeds = load_list(a.addresses)
    if not seeds:
        sys.exit("시드 주소를 찾지 못했습니다.")
    labels = load_labels(a.labels)
    for s in seeds:
        labels.setdefault(s, "FBI seed")

    hop = {s: 0 for s in seeds}
    kind = {}
    edges, seen_edge = [], set()
    q = deque(seeds)
    print(f"시드 {len(seeds)}개, 최대 {a.hops} hop, 새 주소 최대 {a.max_nodes}개 추적 시작")
    est = (len(seeds) + a.max_nodes) * 4 / a.rps / 60
    print(f"예상 소요 시간: 최대 약 {est:.0f}분 (API 호출 한도 {a.rps}회/초 기준)")

    limit = len(seeds) + a.max_nodes   # 시드가 많아도 확장이 막히지 않도록 시드 수는 따로 센다
    while q and len(hop) < limit:
        addr = q.popleft()
        if addr not in kind:
            kind[addr] = "contract" if es.is_contract(addr) else "EOA"
        # 컨트랙트(거래소 핫월렛 제외 대부분 DEX/브릿지)는 확장하지 않고 종착점으로 둔다
        if kind[addr] == "contract" and hop[addr] > 0:
            continue
        if hop[addr] >= a.hops:
            continue
        if addr in labels and hop[addr] > 0 and labels[addr] != "FBI seed":
            continue  # 라벨 붙은 거래소/브릿지 = 종착점
        print(f"[hop {hop[addr]}] {addr}  (노드 {len(hop)}, API {es.calls})")
        outs = outgoing(es, addr, a.since_block, a.min_eth)
        # 수신자별 합산 후 큰 순서로 상위만 확장
        by_dst = defaultdict(float)
        for e in outs:
            by_dst[e["dst"]] += eth_equiv(e)
        keep = set(sorted(by_dst, key=by_dst.get, reverse=True)[: a.max_children])
        for e in outs:
            key = (e["tx"], e["dst"], e["asset"])
            if key in seen_edge:
                continue
            seen_edge.add(key)
            edges.append(e)
            d = e["dst"]
            if d in keep and d not in hop:
                hop[d] = hop[addr] + 1
                q.append(d)

    # 종착 노드 타입 확인
    for addr in list(hop):
        if addr not in kind:
            kind[addr] = "contract" if es.is_contract(addr) else "EOA"

    # 집계
    tin, tout = defaultdict(float), defaultdict(float)
    fns = defaultdict(set)
    for e in edges:
        tout[e["src"]] += eth_equiv(e)
        tin[e["dst"]] += eth_equiv(e)
        if e["fn"]:
            fns[e["dst"]].add(e["fn"])

    def flag(addr):
        fl = []
        if kind.get(addr) == "contract":
            fl.append("contract")
            if any(h in f.lower() for f in fns[addr] for h in EXIT_HINTS):
                fl.append("EXIT_CANDIDATE")
        if addr in labels:
            fl.append("labeled")
        return ";".join(fl)

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "nodes.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["address", "hop", "type", "label", "flags",
                    "in_eth_equiv", "out_eth_equiv", "called_functions", "etherscan"])
        for addr in sorted(hop, key=lambda x: (hop[x], -tin[x])):
            w.writerow([addr, hop[addr], kind.get(addr, ""), labels.get(addr, ""),
                        flag(addr), round(tin[addr], 4), round(tout[addr], 4),
                        "|".join(sorted(fns[addr])),
                        f"https://etherscan.io/address/{addr}"])
    with open(os.path.join(a.out, "edges.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["from", "to", "asset", "amount", "time_utc", "function", "tx", "etherscan"])
        for e in edges:
            w.writerow([e["src"], e["dst"], e["asset"], e["amount"], e["time"], e["fn"],
                        e["tx"], f"https://etherscan.io/tx/{e['tx']}"])
    exits = [x for x in hop if kind.get(x) == "contract" and tin[x] > 0]
    exits.sort(key=lambda x: -tin[x])
    with open(os.path.join(a.out, "exits.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["contract", "label", "in_eth_equiv", "functions", "flags", "etherscan"])
        for x in exits:
            w.writerow([x, labels.get(x, ""), round(tin[x], 4), "|".join(sorted(fns[x])),
                        flag(x), f"https://etherscan.io/address/{x}"])

    write_graph(os.path.join(a.out, "graph.html"), hop, kind, labels, edges, tin, flag)
    print(f"\n완료: 노드 {len(hop)}, 엣지 {len(edges)}, 출구 후보 {len(exits)}, API 호출 {es.calls}")
    print(f"결과: {os.path.abspath(a.out)}/")
    if exits:
        print("\n유입 상위 컨트랙트 (출구 후보):")
        for x in exits[:10]:
            print(f"  {x}  ~{tin[x]:,.1f} ETH-eq  {labels.get(x, '')}  {flag(x)}")


def write_graph(path, hop, kind, labels, edges, tin, flag):
    colors = ["#d62728", "#ff7f0e", "#e6b800", "#2ca02c", "#1f77b4", "#9467bd"]
    nodes = []
    for a_, h in hop.items():
        fl = flag(a_)
        nodes.append({
            "id": a_,
            "label": (labels.get(a_) or a_[:8] + "…") + f"\nhop {h}",
            "title": f"{a_}\n{kind.get(a_, '')} {fl}\n유입 ~{tin[a_]:.2f} ETH-eq",
            "color": "#111" if "EXIT_CANDIDATE" in fl else colors[min(h, len(colors) - 1)],
            "shape": "box" if kind.get(a_) == "contract" else "dot",
            "value": max(1.0, tin[a_]),
        })
    agg = defaultdict(lambda: [0.0, set()])
    for e in edges:
        if e["src"] in hop and e["dst"] in hop:
            k = (e["src"], e["dst"])
            agg[k][0] += eth_equiv(e)
            agg[k][1].add(e["asset"])
    links = [{"from": s, "to": d, "arrows": "to", "value": v,
              "title": f"~{v:,.2f} ETH-eq ({', '.join(sorted(assets))})"}
             for (s, d), (v, assets) in agg.items()]
    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>ETH 자금 흐름</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>body{{margin:0;font-family:sans-serif}}#g{{width:100%;height:720px}}
#h{{padding:8px 12px;font-size:13px;background:#f4f4f4}}</style></head><body>
<div id="h">빨강=시드(hop0) → 주황/노랑/초록=다음 hop · 상자=컨트랙트 · 검정 상자=출구 후보 · 노드 더블클릭 시 Etherscan</div>
<div id="g"></div><script>
const nodes=new vis.DataSet({json.dumps(nodes)});
const edges=new vis.DataSet({json.dumps(links)});
const net=new vis.Network(document.getElementById('g'),{{nodes,edges}},{{
 layout:{{hierarchical:{{direction:'LR',sortMethod:'directed',levelSeparation:260}}}},
 physics:false,nodes:{{font:{{size:11}},scaling:{{min:6,max:40}}}},
 edges:{{scaling:{{min:1,max:12}},smooth:{{type:'cubicBezier'}}}}}});
net.on('doubleClick',p=>{{if(p.nodes.length)window.open('https://etherscan.io/address/'+p.nodes[0])}});
</script></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
