# Crypto Laundering Forensics Lab

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/dasekang/crypto-laundering-forensics-lab/blob/main/forensics_lab_colab.ipynb)

공개 블록체인 데이터로 탈취 암호화폐의 자금 흐름을 추적하고, 세탁 패턴을 지표로 비교하는 교육용 실습 도구입니다.
북한 TraderTraitor의 거래소 탈취 사건(Bybit 2025, Binance 2026)을 사례로 합니다.

An educational toolkit for tracing stolen crypto with public blockchain data and comparing laundering patterns,
using North Korea's TraderTraitor exchange thefts as case studies.

## 실습 구성

| 단계 | 내용 | API 키 |
| --- | --- | --- |
| 1–2 | 합성 데이터로 두 세탁 패턴(빠른 분산형 vs 필 체인형) 비교 | 필요 없음 |
| 3 | 공개된 시드 주소에서 출발해 실제 자금 흐름 추적 | Etherscan 무료 키 |
| 4 | 지갑 모니터링 시트에 잔액·보낸 거래 수를 한 번에 채우기 | Etherscan 무료 키 |

위의 **Open in Colab** 버튼으로 바로 실행할 수 있습니다. 설치는 필요 없습니다.

## 파일

| 경로 | 설명 |
| --- | --- |
| `forensics_lab_colab.ipynb` | Colab 실습 노트북 (스크립트 내장) |
| `make_sample.py` | 합성 세탁 사례 생성 (가짜 데이터) |
| `trace_flows.py` | 시드 주소에서 N-hop 자금 흐름 추적 (Etherscan API V2) |
| `analyze.py` | 세탁 지표 계산, 비교 리포트(HTML)와 블로그용 리포트 생성 |
| `addresses_template.txt` | 시드 주소 입력 양식 |
| `binance_wallet_monitor_template.xlsx` | 4단계용 지갑 모니터링 시트 양식 |

로컬에서 실행하려면:

```bash
python3 make_sample.py --seed 7
python3 analyze.py A=sample/case_A B=sample/case_B --out report

export ETHERSCAN_API_KEY=your_key
python3 trace_flows.py addresses_template.txt --hops 2
```

## 지표

출구 도달 비율, 첫 출구까지 걸린 시간, T50/T90, 평균 팬아웃, 경유 지갑 비율, 최장 경유 체인, 중앙 보유 시간, 자산 구성.
정의는 `analyze.py` 상단 주석을 참고하세요.

## 연구 윤리와 안전

- 공개 데이터를 **조회만** 합니다. 공개된 주소로 송금하거나 상호작용하지 마세요.
- 분석 결과는 단서일 뿐 사실 확정이 아닙니다. 특정 개인·기업을 공개적으로 지목하지 마세요.
- API 키는 Colab 보안 비밀 또는 환경변수로만 다루고, 코드나 노트북에 적지 마세요.
- 의미 있는 발견은 트랜잭션 해시를 근거로 수사기관(tips.fbi.gov 등)에 제보하세요.

## 데모

합성 데이터 비교 리포트: https://huggingface.co/spaces/greatkoko/crypto-laundering-patterns

## 저자

Dongsu Kang, Korea National Defense University
