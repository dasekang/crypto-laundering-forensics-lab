#!/usr/bin/env python3
"""
make_sample.py - API 키 없이 실습할 수 있는 '합성' 세탁 사례 데이터를 만든다.
주소와 거래는 모두 무작위로 생성된 가짜이며 실제 블록체인과 무관하다.

  사례 A (빠른 분산형): 탈취 직후 수십 개 지갑으로 흩뿌린 뒤 몇 시간 안에
                        브릿지·스왑으로 빠져나가는 패턴
  사례 B (필 체인형):   한 지갑에서 다음 지갑으로 대부분을 넘기며 매 단계 일부만
                        떼어 출구로 보내는 느린 패턴

출력 폴더 형식은 trace_flows.py와 같다 (nodes.csv, edges.csv).
사용법:  python3 make_sample.py [--out sample] [--seed 7]
"""
import argparse
import csv
import os
import random
from datetime import datetime, timedelta, timezone

EXITS = {
    "SampleBridge (가상)": "bridgeOut",
    "SampleSwap (가상)": "swapExactETHForTokens",
    "SampleRouter (가상)": "multicall",
}


class Case:
    def __init__(self, rng, start):
        self.rng, self.start = rng, start
        self.nodes, self.edges = {}, []
        self.exits = {}
        for label, fn in EXITS.items():
            a = self.addr()
            self.exits[label] = (a, fn)

    def addr(self):
        return "0x" + "".join(self.rng.choice("0123456789abcdef") for _ in range(40))

    def node(self, a, hop, kind="EOA", label=""):
        if a not in self.nodes:
            self.nodes[a] = dict(hop=hop, type=kind, label=label)
        return a

    def send(self, src, dst, amount, t, asset="ETH", fn=""):
        self.edges.append(dict(src=src, dst=dst, asset=asset, amount=round(amount, 6),
                               t=t, fn=fn, tx="0x" + "".join(self.rng.choice("0123456789abcdef") for _ in range(64))))

    def to_exit(self, src, amount, t, hop):
        label = self.rng.choices(list(self.exits), weights=[5, 3, 2])[0]
        a, fn = self.exits[label]
        self.node(a, hop, "contract", label)
        asset = "ETH"
        if fn.startswith("swap") and self.rng.random() < 0.3:
            asset, fn = "WETH", ""
        self.send(src, a, amount, t, asset, fn)

    def write(self, folder):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "nodes.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["address", "hop", "type", "label", "flags"])
            for a, n in sorted(self.nodes.items(), key=lambda kv: kv[1]["hop"]):
                flags = "contract;EXIT_CANDIDATE" if n["type"] == "contract" else ""
                w.writerow([a, n["hop"], n["type"], n["label"], flags])
        with open(os.path.join(folder, "edges.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["from", "to", "asset", "amount", "time_utc", "function", "tx"])
            for e in sorted(self.edges, key=lambda e: e["t"]):
                w.writerow([e["src"], e["dst"], e["asset"], e["amount"],
                            e["t"].strftime("%Y-%m-%d %H:%M:%S"), e["fn"], e["tx"]])


def case_fast(rng, start):
    """사례 A: 시드 3개 → 각 20개 지갑 → 각 2~5개 → 대부분 수 시간 내 출구"""
    c = Case(rng, start)
    for s in range(3):
        seed = c.node(c.addr(), 0, label="seed (가상)")
        total = rng.uniform(30_000, 50_000)
        t = start + timedelta(minutes=rng.uniform(0, 10))
        shares = [rng.random() for _ in range(20)]
        for sh in shares:
            amt = total * sh / sum(shares)
            h1 = c.node(c.addr(), 1)
            t1 = t + timedelta(minutes=rng.uniform(1, 25))
            c.send(seed, h1, amt, t1)
            k = rng.randint(2, 5)
            parts = [rng.random() for _ in range(k)]
            for p in parts:
                a2 = amt * p / sum(parts)
                h2 = c.node(c.addr(), 2)
                t2 = t1 + timedelta(minutes=rng.uniform(5, 60))
                c.send(h1, h2, a2, t2)
                t3 = t2 + timedelta(minutes=rng.expovariate(1 / 90))
                if rng.random() < 0.85:
                    c.to_exit(h2, a2 * rng.uniform(0.95, 1.0), t3, 3)
                else:  # 일부는 스테이블코인으로 바꿔 다른 지갑에 보관
                    h3 = c.node(c.addr(), 3)
                    c.send(h2, h3, a2 * 3000, t3, asset="USDT")
    return c


def case_peel(rng, start):
    """사례 B: 시드 1개에서 12단계 필 체인, 매 단계 8~15%를 출구로"""
    c = Case(rng, start)
    cur = c.node(c.addr(), 0, label="seed (가상)")
    bal = rng.uniform(80_000, 100_000)
    t = start
    for hop in range(1, 13):
        t += timedelta(minutes=rng.uniform(60, 300))
        peel = bal * rng.uniform(0.08, 0.15)
        c.to_exit(cur, peel, t, hop)
        bal -= peel
        nxt = c.node(c.addr(), hop)
        t += timedelta(minutes=rng.uniform(2, 20))
        # 가끔 곁가지 지갑으로 소액 분리 (다음 지갑으로 넘기기 전에)
        if rng.random() < 0.3:
            side = c.node(c.addr(), hop)
            amt = bal * 0.05
            bal -= amt
            c.send(cur, side, amt, t + timedelta(minutes=1))
            c.to_exit(side, amt, t + timedelta(hours=rng.uniform(3, 12)), hop + 1)
        c.send(cur, nxt, bal * 0.999, t + timedelta(minutes=2))
        bal *= 0.999
        cur = nxt
    c.to_exit(cur, bal, t + timedelta(hours=2), 13)
    return c


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sample")
    ap.add_argument("--seed", type=int, default=7, help="난수 시드 (같은 값이면 같은 데이터)")
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    start = datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc)
    case_fast(rng, start).write(os.path.join(a.out, "case_A"))
    case_peel(rng, start).write(os.path.join(a.out, "case_B"))
    print(f"합성 데이터 생성: {os.path.abspath(a.out)}/case_A, case_B")
    print(f"다음 단계: python3 analyze.py 사례A={a.out}/case_A 사례B={a.out}/case_B")


if __name__ == "__main__":
    main()
