# Crypto Laundering Forensics Lab

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/dasekang/crypto-laundering-forensics-lab/blob/main/forensics_lab_colab.ipynb)

An educational toolkit for tracing stolen crypto with public blockchain data and comparing laundering patterns.
Case studies: North Korea's TraderTraitor exchange thefts (Bybit 2025, Binance 2026).

> The notebook and script messages are currently in Korean. Code, variable names, and outputs (CSV, charts) are language-neutral.

## What it does

| Step | Task | API key |
| --- | --- | --- |
| 1–2 | Compare two laundering patterns (rapid dispersal vs. peel chain) on synthetic data | Not needed |
| 3 | Trace fund flows N hops out from public seed addresses | Free Etherscan key |
| 4 | Fill a wallet-monitoring sheet with current balances and sent-transaction counts | Free Etherscan key |

Click **Open in Colab** above to run it in your browser. No installation needed.
For steps 3–4, add your key in Colab's **Secrets** (🔑 in the left sidebar) as `ETHERSCAN_API_KEY`.

## Files

| File | Description |
| --- | --- |
| `forensics_lab_colab.ipynb` | Colab notebook (scripts embedded) |
| `make_sample.py` | Generates two synthetic laundering cases (fake data) |
| `trace_flows.py` | N-hop flow tracing from seed addresses (Etherscan API V2) |
| `analyze.py` | Laundering metrics, comparison report (HTML), and a blog-ready static report |
| `addresses_template.txt` | Seed address input template |
| `binance_wallet_monitor_template.xlsx` | Wallet-monitoring sheet for step 4 |

Run locally:

```bash
python3 make_sample.py --seed 7
python3 analyze.py A=sample/case_A B=sample/case_B --out report

export ETHERSCAN_API_KEY=your_key
python3 trace_flows.py addresses_template.txt --hops 2
```

## Metrics

Exit share, time to first exit, T50/T90 (time until 50%/90% of exit-bound funds arrive), mean fan-out,
pass-through wallet ratio, longest pass-through chain, median holding time, and asset mix.
Definitions are in the header comment of `analyze.py`.

## Ethics and safety

- Read-only. Do not send funds to or interact with any listed address.
- Results are leads, not conclusions. Do not publicly accuse individuals or companies.
- Keep API keys in Colab Secrets or environment variables, never in code or notebooks.
- Report meaningful findings, with transaction hashes, to law enforcement (e.g., tips.fbi.gov).

## Demo

Synthetic-data comparison report: https://huggingface.co/spaces/greatkoko/crypto-laundering-patterns

---

## 한국어 안내

공개 블록체인 데이터로 탈취 암호화폐의 자금 흐름을 추적하고, 세탁 패턴을 지표로 비교하는 교육용 실습 도구입니다.
위의 **Open in Colab** 버튼으로 바로 실행할 수 있습니다. 1–2단계는 API 키 없이, 3–4단계는 Etherscan 무료 키를
Colab 보안 비밀(`ETHERSCAN_API_KEY`)에 등록해 사용합니다. 공개 데이터를 조회만 하며, 분석 결과는 단서일 뿐 사실 확정이 아닙니다.

## Author

Dongsu Kang, Korea National Defense University
