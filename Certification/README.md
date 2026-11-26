# 이기종 GPU 클러스터 LLM 서빙 처리량 향상율 평가

동일한 GPU 클러스터(= 동일 예산)를 두 가지 방식으로 서빙했을 때의 처리량을 비교한다.

| 구성 | 설명 |
|---|---|
| 기준 구성 | 기존 서빙 시스템(vLLM) 방식. GPU 종류별로 묶어 동종 GPU 파이프라인을 만들고 레이어를 균등 분할한다. |
| 이기종 GPU 구성 | ShuntServe 옵티마이저가 서로 다른 GPU 종류를 한 파이프라인에 섞어 배치한다. |

**합격 기준:** 이기종 GPU 구성의 처리량이 기준 구성보다 50% 이상 높다.

인프라 생성부터 결과 회수까지 처음부터 끝까지 따라 할 수 있는 절차는 [`AGENT.md`](AGENT.md)에 있다.

## 평가 조건

| 항목 | 내용 |
|---|---|
| 모델 | Llama-3.1-70B-Instruct |
| 클러스터 | g6.12xlarge×2 (L4 8장), g5.12xlarge×2 (A10G 8장), g6e.xlarge×4 (L40S 4장). GPU 3종, 총 8대 |
| 데이터셋 | Azure LLM Inference Conversation Trace 2023. 입력 2048 토큰 초과 요청을 제외한 뒤 처음 3분 구간(727개 요청)을 사용. 정제한 파일은 [`dataset/`](dataset/README.md)에 있다 |
| 측정 방식 | 모든 요청을 한 번에 제출하고, 완료 요청 수 ÷ 전체 소요 시간으로 처리량(req/s)을 계산 |

두 구성은 같은 8대, 같은 모델, 같은 요청을 사용하며 서빙 구성만 다르다.

## 사전 준비 (평가를 시작하기 전에 반드시 먼저 한다)

### 갖춰져 있어야 하는 것

1. 클러스터 8대와 head 노드가 실행 중이고, 모든 노드에 ShuntServe가 `/home/ubuntu/ShuntServe`에 설치되어 있다. 클러스터는 [`IaC/`](IaC/README.md)의 Terraform 구성으로 만든다.
2. head 노드에서 모든 노드로 키 인증으로 SSH 접속이 된다. 처음 접속할 때의 host fingerprint 확인은 스크립트가 건너뛰므로 미리 접속해 둘 필요는 없다.
3. 모델 가중치가 S3에 올라가 있고(`TensorStore/upload_model.sh`), 노드에서 읽을 수 있다.
4. head 노드와 모든 노드에 `HF_TOKEN`이 설정되어 있다.
5. [`config.json`](config.json)의 `s3_bucket`에 버킷 이름을 적는다. 저장소에는 비워 둔 채로 올라가 있으며, 적은 값은 커밋하지 않는다. 노드 IP는 아래 1번 단계에서 채운다.

### 1. 설정과 코드를 head로 보내기 (로컬, 저장소 루트에서 실행)

head의 `Certification/`은 AMI를 만든 시점의 것이고, `config.json`의 노드 IP도 비어 있다.
노드 IP를 로컬 `config.json`에 기록한 뒤, 로컬의 `Certification/` 폴더를 rsync로 head에 그대로 맞춘다.

```bash
python3 Certification/IaC/fill_config.py      # terraform output의 노드 private IP를 config.json에 기록
HEAD=$(terraform -chdir=Certification/IaC output -raw head_instance_public_ip)
SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
EXCL=(--exclude 'IaC/' --exclude 'results/' --exclude '__pycache__/' --exclude '.DS_Store')

# 바뀌거나 지워질 파일을 미리 본다 (아무것도 바꾸지 않음)
rsync -az --delete --dry-run --itemize-changes "${EXCL[@]}" -e "$SSH" Certification/ ubuntu@$HEAD:~/ShuntServe/Certification/

# 실제로 맞춘다
rsync -az --delete "${EXCL[@]}" -e "$SSH" Certification/ ubuntu@$HEAD:~/ShuntServe/Certification/
```

- 인프라 파일(`IaC/`, HuggingFace 토큰 포함)과 결과 폴더(`results/`)는 보내지 않으며, head에 있는 이 두 폴더는 건드리지 않는다.
- `--delete` 때문에 그 밖에 head에만 있는 파일은 지워진다. head의 `Certification/` 아래에는 `results/` 말고 직접 파일을 만들어 두지 않는다.
- 로컬에서 `Certification/`의 파일을 고쳤다면 다시 실행한다. `Certification/`만 바뀐 경우에는 AMI를 다시 만들 필요가 없다.
- 워커 노드에는 아무것도 보내지 않는다. 워커는 AMI에 든 코드를 그대로 쓴다.

### 2. 준비 스크립트 (head 노드에서 실행)

```bash
cd ~/ShuntServe/Certification

./prepare/setup_ray.sh    # Ray head를 켜고 config.json의 모든 노드를 Ray 클러스터에 연결
./prepare/warmup.sh       # 두 구성을 각각 한 번씩 띄워 짧게 서빙한 뒤 내림
```

| 스크립트 | 내용 |
|---|---|
| `prepare/setup_ray.sh` | 먼저 모든 노드가 떠 있고 SSH 접속이 되는지 확인한다(안 되는 노드는 이유와 함께 알려 주고 중단). 이어서 head에서 Ray를 시작하고, 각 노드에 SSH로 접속해 Ray 클러스터에 연결한 뒤 노드별 GPU 수를 확인한다. 이미 연결된 노드는 건드리지 않으므로 다시 실행해도 된다. `--restart`는 전부 내렸다가 다시 시작, `--stop`은 전부 내린다 |
| `prepare/warmup.sh` | 모델 배치를 계산하고, 기준 구성과 이기종 GPU 구성을 각각 한 번 띄워 평가 데이터셋의 처음 30초 구간(52개 요청)을 처리한 뒤 내린다. 새로 띄운 노드는 디스크의 소프트웨어를 처음 읽을 때 느리므로, 모든 노드가 소프트웨어와 모델을 한 번씩 올려 보게 하기 위한 것이다. 약 20분 |

준비 스크립트의 출력은 `results/prepare/`에 따로 저장되며 평가 결과에는 쓰이지 않는다.
준비가 끝났는지는 평가 절차의 1단계(`./1_check_environment.sh`)로 확인한다.

## 평가 절차

사전 준비가 끝난 뒤 Ray head 노드에서 실행한다. 단계별 스크립트를 순서대로 실행하거나, `run_all.sh`로 한 번에 실행한다.

```bash
cd ~/ShuntServe/Certification

./1_check_environment.sh      # 환경 확인
./2_optimize.sh               # 모델 배치 계산
./3_benchmark_vllm.sh         # 기준 구성 측정
./4_benchmark_shuntserve.sh   # 이기종 GPU 구성 측정
./5_report.sh                 # 두 구성 비교

./run_all.sh                  # 위 다섯 단계를 순서대로 실행 (실패하면 그 단계에서 멈춤)
```

| 단계 | 내용 | 예상 소요 |
|---|---|---|
| 1. `1_check_environment.sh` | 평가 환경 출력: 인스턴스 종류, GPU 수, 노드 수, 모델과 가중치 크기. 노드 접속, 실제 인스턴스 종류와 GPU 수, S3의 모델 가중치, 토큰, Ray 상태는 내부적으로 점검하고 문제가 있을 때만 출력한다 | 1분 이내 |
| 2. `2_optimize.sh` | 두 구성의 모델 배치 계산 (GPU 불필요) | 약 1분 (워밍업 전 첫 실행은 약 3분) |
| 3. `3_benchmark_vllm.sh` | 기준 구성으로 파이프라인을 띄우고 처리량 측정 | 약 25분 |
| 4. `4_benchmark_shuntserve.sh` | 이기종 GPU 구성으로 파이프라인을 띄우고 처리량 측정 | 약 15~20분 |
| 5. `5_report.sh` | 두 결과를 비교해 처리량 비율과 향상율 출력 | 즉시 |

2단계는 head(m5.xlarge)에서 잰 값이고, 3, 4단계의 소요 시간은 실측이 아니라 추정치다.

1단계를 실행할 때마다 새 결과 폴더가 만들어지고, 2~5단계는 가장 최근 결과 폴더에 이어서 기록한다.
어떤 단계가 실패하면 원인을 고친 뒤 그 단계부터 다시 실행하면 된다.

## 결과 위치

`results/<1단계 실행 시각>/` 폴더에 모든 결과가 모이고, `results/latest`가 가장 최근 폴더를 가리킨다.

```
results/<실행 시각>/
├── report.md                      최종 결과 (평가 조건, 서빙 구성, 측정값, 처리량 비율과 향상율)
├── report.json                    같은 내용의 수치 데이터
├── placement/predicted_*.json     옵티마이저가 계산한 파이프라인 구성
├── measured/offline_*.json        처리량과 지연 시간 측정값
├── Trace/offline_*.csv            요청별 기록 (도착, 완료, 토큰 수, 지연 시간)
└── logs/
    ├── 1_check_environment.log ... 5_report.log   각 단계의 화면 출력
    └── {vllm,shuntserve}/         GlobalServer, Cluster 로그와 각 노드의 TensorStore, 추론 엔진 로그 (remote/)
```

## 설정 파일 (`config.json`)

| 항목 | 의미 |
|---|---|
| `nodes` | 인스턴스 종류별 노드 private IP 목록. 옵티마이저와 벤치마크가 모두 이 목록을 클러스터로 사용한다 |
| `s3_bucket` | 모델 가중치가 있는 S3 버킷 |
| `workload.dataset` | 평가 데이터셋 경로. 3분(현재)과 5분 파일이 있다 ([`dataset/`](dataset/README.md)) |
| `workload.window_s` | 데이터셋이 trace의 처음 몇 초 구간인지. 리포트 문구에 쓰인다 |
| `workload.expected_*_len` | 옵티마이저가 가정하는 평균 입출력 길이 |
| `warmup.end_time_s` | `prepare/warmup.sh`가 쓰는 구간. 평가 데이터셋의 처음 몇 초까지 쓸지 정한다 |
| `target_improvement_pct` | 합격 기준 (처리량 향상율, %) |

파이프라인의 각 단계에 어떤 노드를 쓸지는 옵티마이저 결과의 인스턴스 종류를 보고 자동으로 정해진다.
실제로 띄우지 않고 배정만 확인하려면 다음을 실행한다.

```bash
python3 scripts/benchmark.py --system shuntserve --dry-run
```

## 문제가 생겼을 때

- **파이프라인이 준비되지 않는 경우:** 준비를 기다리는 동안 30초마다 경과 시간과 가장 최근 노드 로그 한 줄이 출력된다. 같은 줄에서 오래 멈춰 있으면 Ctrl-C로 중단하고 `results/latest/logs/<구성>/remote/`의 로그를 확인한다. 파이프라인 생성이 오류로 끝난 경우에는 각 노드 로그의 마지막 부분이 화면에 출력되고 실행이 중단된다.
- **중간에 멈추고 다시 시작하는 경우:** 워밍업과 벤치마크 단계는 시작할 때 노드에 남아 있는 서빙 프로세스를 정리하므로, 같은 단계를 다시 실행하면 된다. 1단계를 다시 실행하면 새 결과 폴더가 만들어지므로, 이어서 하려면 실패한 단계부터 실행한다.
- **결과에 `WARNING: this result is not valid`가 나온 경우:** 한쪽 실행에서 모든 요청이 완료되지 않은 것이다. 해당 측정 단계(3 또는 4)와 5단계를 다시 실행한다.

## 구현 참고

- 옵티마이저와 벤치마크는 논문 실험 코드(`ModelPlacement/optimizer/llama3-70b/`, `ModelPlacement/offline/llama3-70b/`)를 옮겨온 것이며, 클러스터 구성을 `config.json`에서 읽는 점만 다르다.
- 요청 라우팅은 GlobalServer의 기존 방식 그대로다. 여유가 있는 파이프라인 중에서, 옵티마이저가 파이프라인마다 계산한 가중치에 따라 선택한다.
