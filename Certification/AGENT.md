# 인증평가 진행 절차 (에이전트용)

이 문서는 이 저장소를 받은 에이전트(또는 처음 진행하는 사람)가 **인프라 생성부터 결과 회수까지** 그대로 따라 할 수 있도록 쓴 절차서다.
평가 내용과 각 스크립트의 설명은 [`README.md`](README.md)에 있다. 이 문서는 "무엇을, 어떤 순서로, 어떻게 확인하며" 실행하는지만 다룬다.

## 0. 먼저 읽을 것: 지켜야 할 규칙

1. **설정과 코드를 임의로 고치지 않는다.** 고쳐도 되는 것은 `config.json`의 `nodes`(노드 IP)뿐이며, 이것도 `IaC/fill_config.py`로만 채운다. 데이터셋, 합격 기준, 스크립트를 바꿔야 할 것 같으면 멈추고 사람에게 묻는다.
2. **비밀 값을 출력하지 않는다.** `IaC/terraform.tfvars`에는 HuggingFace 토큰이 들어 있다. 파일 내용을 화면이나 로그에 찍지 않는다.
3. **비용이 드는 작업은 사람의 승인을 받는다.** `terraform apply`(시간당 약 $28), 가용 영역(AZ) 변경, `terraform destroy`, 용량 예약 생성이 해당한다.
4. **오래 걸리는 작업은 head 노드의 `tmux` 안에서 실행한다.** SSH 연결이 끊겨도 작업이 계속되게 하기 위해서다.
5. **완료 여부는 종료 코드 파일이나 로그로 확인한다.** `pgrep -f "<스크립트 이름>"` 같은 방식은 확인하는 명령 자신의 명령줄에도 걸려서, 끝난 작업을 계속 "실행 중"으로 잘못 판단할 수 있다.
6. **노드의 프로세스를 직접 죽이지 않는다.** 서빙 프로세스 정리는 벤치마크 스크립트가 시작과 끝에 스스로 한다.

## 1. 준비물 확인 (로컬)

에이전트가 일하는 로컬 머신에 아래가 있어야 한다.

| 항목 | 확인 방법 |
|---|---|
| 이 저장소 | 저장소 루트에 `.git`이 있음 (스크립트가 `.git` 위치로 루트를 찾는다) |
| Terraform 1.5 이상, AWS CLI | `terraform version`, `aws --version` |
| AWS 프로필 | `IaC/terraform.tfvars`의 `awscli_profile`로 `aws sts get-caller-identity`가 성공 |
| `IaC/terraform.tfvars` | 없으면 `terraform.tfvars.sample`을 복사해 사람에게 값을 받아 채운다. `ami_id`는 ShuntServe가 설치된 AMI여야 한다 |
| head 노드 SSH 키 | head 노드에 `ubuntu` 계정으로 키 인증 접속이 가능한 개인 키 |
| S3의 모델 가중치 | `aws s3 ls s3://<config.json의 s3_bucket>/raw/meta-llama/Llama-3.1-70B-Instruct/` 아래에 `TP1/`, `TP4/`, `config.json`이 있음 |

이 문서의 명령은 아래 두 가지를 쓴다. 처음 접속할 때의 host fingerprint 질문을 건너뛰고, 키 인증이 안 되면 묻지 않고 실패한다.
`on_head`는 head에서 명령을 실행하는 함수이고, `SSH`는 rsync의 `-e`에 넘기는 문자열이다.
문자열 변수를 `$SSH ...`처럼 명령으로 쓰면 zsh에서는 공백에서 나뉘지 않아 실행되지 않으므로, head에 직접 명령을 보낼 때는 항상 `on_head`를 쓴다.
`HEAD`(head의 주소)는 3장에서 정한다.

```bash
SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes"
on_head() { ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes "ubuntu@$HEAD" "$@"; }
```

## 2. 인프라 생성 (로컬, 승인 필요)

GPU 용량이 빠듯해서 바로 `terraform apply`를 하면 일부만 뜬 채로 나머지를 몇 시간씩 기다리며 요금만 낼 수 있다.
그래서 먼저 구하기 어려운 g6.12xlarge×2, g5.12xlarge×2의 용량을 한 AZ에 통째로 예약하고, 예약이 잡힌 AZ로 인프라를 만든다.
미리 온디맨드 가용성을 알아보는 방법은 없다. Spot Placement Score는 spot 기준이라 쓰지 않는다.

### 2-1. 용량 예약 (승인 필요)

```bash
cd Certification/IaC
./reserve_capacity.sh --dry-run                    # 권한과 파라미터만 확인. 예약하지 않으며 무료
./reserve_capacity.sh 2>&1 | tee reserve.log       # 사람의 승인 후 실행. 성공할 때까지 반복한다
```

- us-west-2a, b, c 순서로 AZ마다 g6.12xlarge와 g5.12xlarge를 `config.json`의 대수만큼 예약해 본다(둘 다 잡히면 그 예약은 유지한 채 같은 AZ에서 g6e.xlarge×4가 잡힐 때까지 10초 간격으로 다시 시도한다. 이때부터 요금이 나간다). 하나라도 실패하면 그 AZ에서 방금 잡은 예약을 바로 취소하고 다음 AZ로 넘어간다. 세 AZ가 모두 실패하면 60초 뒤 다시 시도한다(`INTERVAL=120`처럼 바꿀 수 있다). AZ를 지정하려면 `./reserve_capacity.sh us-west-2b us-west-2c`처럼 인자로 준다.
- 실패한 시도는 무료다. 예약은 잡힌 순간부터 취소할 때까지 온디맨드 요금이 나간다. 그 안에서 인스턴스가 돌면 이중으로 과금되지는 않는다.
- 성공하면 예약된 AZ, 예약 ID, 다음 할 일을 출력하고 끝난다. 용량 부족이 아닌 오류(권한, 한도 등)가 나면 잡은 예약을 취소하고 멈춘다. 두 경우 모두 데스크톱 알림이 뜬다.
- 중간에 Ctrl-C로 멈추면 그때 시도 중이던 AZ의 예약을 취소하고 끝난다.
- 이미 이 스크립트의 활성 예약이 있으면 시작하지 않는다. `--list`로 확인한 뒤 사람에게 묻는다.

### 2-2. 인프라 생성 (승인 필요)

`terraform.tfvars`의 `availability_zone`을 예약이 잡힌 AZ로 바꾼다. AZ 변경도 승인이 필요하다(0장 3번).
이 한 줄만 고치고, 파일 내용은 출력하지 않는다(토큰이 들어 있다). 확인은 `grep '^availability_zone' terraform.tfvars`로 한다.

```bash
terraform init
terraform apply          # 사람의 승인 후 실행
```

워커는 같은 종류, 같은 AZ의 예약에 자동으로 들어간다. head(m5.xlarge)는 예약하지 않는다.

**성공 기준:** head 1대와 워커 8대(g6.12xlarge 2, g5.12xlarge 2, g6e.xlarge 4)가 모두 만들어진다.
`terraform state list | grep aws_instance`가 9줄이어야 한다.

### 2-3. 예약 취소

워커가 모두 running이 되면 예약을 취소한다. 돌고 있는 인스턴스는 그대로 돌고 예약만 없어진다.
`terraform destroy`는 예약을 지우지 않으므로, 남겨 두면 인스턴스를 지운 뒤에도 요금이 계속 나간다.

```bash
./reserve_capacity.sh --cancel
./reserve_capacity.sh --list         # "No active reservations made by this script."가 나와야 한다
```

**apply 중 `InsufficientInstanceCapacity`가 나는 경우:**
- `terraform.tfvars`의 AZ가 예약된 AZ와 같은지, `--list`에서 예약이 active인지 확인한다. 예약 없이 apply했다면 2-1부터 한다.
- 이미 뜬 인스턴스는 그대로 두고 사람에게 알린다. AZ를 바꾸면 이미 뜬 인스턴스도 다시 만들어지므로 사람이 결정한다.

## 3. 설정과 코드를 head 노드로 보내기 (로컬)

```bash
cd Certification/IaC
python3 fill_config.py                                   # 워커 private IP를 ../config.json에 기록
HEAD=$(terraform output -raw head_instance_public_ip)
cd ../..                                                 # 저장소 루트
```

AMI 안의 코드는 AMI를 만든 시점의 것이고, `config.json`의 노드 IP는 비어 있다. 로컬의 `Certification/` 폴더를 rsync로 head에 맞춘다.
인프라 파일(`IaC/`, HuggingFace 토큰 포함)과 결과 폴더(`results/`)는 보내지 않으며, head에 있는 이 두 폴더는 건드리지 않는다.
`--delete` 때문에 그 밖에 **head에만 있는 파일은 지워진다**. head의 `Certification/` 아래에는 `results/` 말고 직접 파일을 만들어 두지 않는다.

```bash
EXCL=(--exclude 'IaC/' --exclude 'results/' --exclude '__pycache__/' --exclude '.DS_Store')

# 먼저 무엇이 바뀌고 지워지는지 확인한다 (아무것도 바꾸지 않음)
rsync -az --delete --dry-run --itemize-changes "${EXCL[@]}" -e "$SSH" Certification/ ubuntu@$HEAD:~/ShuntServe/Certification/

# 지워질 항목(*deleting)이 예상 밖이면 멈추고 사람에게 묻는다. 괜찮으면 실제로 맞춘다
rsync -az --delete "${EXCL[@]}" -e "$SSH" Certification/ ubuntu@$HEAD:~/ShuntServe/Certification/
```

head에 rsync가 없다는 오류가 나면 `on_head 'sudo apt-get install -y rsync'`로 설치한 뒤 다시 실행한다.

**성공 기준:** 내용 비교(`-c`) 기준의 dry run이 아무것도 출력하지 않는다.

```bash
rsync -azc --delete --dry-run --itemize-changes "${EXCL[@]}" -e "$SSH" Certification/ ubuntu@$HEAD:~/ShuntServe/Certification/
```

이후 단계는 모두 head 노드에서 실행한다. 워커 노드에는 아무것도 보내지 않는다.

## 4. 사전 준비 (head, 평가 전에 반드시)

### 4-1. Ray 클러스터 구성

```bash
on_head 'cd ~/ShuntServe/Certification && ./prepare/setup_ray.sh'
```

**성공 기준:** 마지막 줄이 `Ray cluster is ready: head + 8 workers, 20 GPUs.`이다.
SSH가 안 되는 노드가 있으면 노드별 이유가 출력되고 중단된다. 노드가 부팅 중이면 몇 분 뒤 다시 실행한다.

### 4-2. 워밍업 (약 20분)

새로 띄운 노드는 디스크의 소프트웨어를 처음 읽을 때 느리다. 평가 전에 두 구성을 한 번씩 띄워 둔다.

```bash
on_head 'rm -f ~/warmup.exit; tmux new-session -d -s warmup \
  "cd ~/ShuntServe/Certification && ./prepare/warmup.sh; echo \$? > ~/warmup.exit"'
```

진행 확인과 완료 확인:

```bash
on_head 'cat ~/warmup.exit 2>/dev/null || echo running; \
  tail -c 2000 ~/ShuntServe/Certification/results/prepare/logs/warmup_*.log | tr "\r" "\n" | tail -5'
```

**성공 기준:** `~/warmup.exit`의 값이 `0`이고, 두 구성 모두 `Completed requests : 52 / 52`가 찍힌다.
워밍업 결과는 `results/prepare/`에 남으며 평가에는 쓰지 않는다.

## 5. 평가 실행 (head)

평가자가 화면을 보며 진행할 때는 README의 단계별 스크립트를 터미널에서 하나씩 실행한다. 에이전트가 대신 진행할 때는 아래처럼 `tmux`에서 한 번에 실행한다.

```bash
on_head 'rm -f ~/cert.exit; tmux new-session -d -s cert \
  "cd ~/ShuntServe/Certification && ./run_all.sh; echo \$? > ~/cert.exit"'
```

진행 확인 (10분 정도 간격):

```bash
on_head 'cat ~/cert.exit 2>/dev/null || echo running; \
  ls ~/ShuntServe/Certification/results/latest/logs/; \
  tail -c 2000 $(ls -t ~/ShuntServe/Certification/results/latest/logs/*.log | head -1) | tr "\r" "\n" | tail -5'
```

`~/cert.exit`의 값: `0`은 정상 완료, `1`은 어떤 단계가 실패, `2`는 결과가 유효하지 않음(한쪽 측정에서 일부 요청이 완료되지 않음)이다.
합격 여부(향상율 50% 이상)는 화면에 출력하지 않는다. `results/latest/report.json`의 `verdict`와 `improvement_pct`로 확인한다.

### 단계별 기대 결과

| 단계 | 정상일 때 | 예상 소요 |
|---|---|---|
| 1. 환경 확인 | `Model: meta-llama/Llama-3.1-70B-Instruct`, `Weights : 141.1 GB`, `Cluster : 8 nodes, 20 GPUs, 3 GPU types`, 마지막 줄 `Environment check passed.` | 1분 이내 |
| 2. 배치 계산 | 아래 "배치 확인" 표와 일치 | 약 1분 (워밍업 전 첫 실행은 약 3분) |
| 3. 기준 구성 측정 | `Completed requests : 727 / 727` | 약 25분 |
| 4. 이기종 GPU 구성 측정 | `Completed requests : 727 / 727` | 약 15~20분 |
| 5. 리포트 | `Throughput ratio`와 `Throughput improvement`가 출력됨 | 즉시 |

3, 4단계 소요 시간은 추정치다.

### 배치 확인 (2단계 결과)

배치는 결정적으로 계산되므로 매번 같아야 한다. 다르면 클러스터 구성이나 코드가 달라진 것이니 진행을 멈추고 사람에게 알린다.

| 구성 | 파이프라인 | 레이어 분할 | 최대 배치 |
|---|---|---|---|
| 기준 (vLLM) | g6.12xlarge × 2 | 40, 40 | 30 |
| 기준 (vLLM) | g5.12xlarge × 2 | 40, 40 | 30 |
| 기준 (vLLM) | g6e.xlarge × 4 | 20, 20, 20, 20 | 11 |
| 이기종 (ShuntServe) | g6.12xlarge × 2 → g6e.xlarge × 4 | 14, 14, 13, 13, 13, 13 | 244 |
| 이기종 (ShuntServe) | g5.12xlarge × 2 | 40, 40 | 30 |

## 6. 결과 회수 (로컬)

```bash
RUN=$(on_head 'basename $(readlink -f ~/ShuntServe/Certification/results/latest)')
mkdir -p Certification/results
on_head "cd ~/ShuntServe/Certification/results && tar -czf - $RUN" | tar -xzf - -C Certification/results
cat Certification/results/$RUN/report.md
```

사람에게는 다음을 보고한다: 결과 폴더 이름, 두 구성의 처리량과 완료 요청 수, 처리량 비율과 향상율, 목표(50% 이상) 충족 여부(`report.json`의 `verdict`), 그리고 중간에 다시 실행한 단계가 있었다면 그 이유.

## 7. 정리 (승인 필요)

평가가 끝나도 클러스터는 사람이 지시하기 전까지 그대로 둔다. 다시 측정해야 할 수 있기 때문이다. 사람이 승인하면 다음을 실행한다.

```bash
on_head 'cd ~/ShuntServe/Certification && ./prepare/setup_ray.sh --stop'
cd Certification/IaC && terraform destroy
./reserve_capacity.sh --cancel       # 2-3에서 취소하지 못한 예약이 남아 있으면 취소
./reserve_capacity.sh --list         # "No active reservations made by this script."가 나와야 한다
```

## 문제 대응

| 증상 | 원인 | 대응 |
|---|---|---|
| `terraform apply`에서 `InsufficientInstanceCapacity` | 해당 AZ에 그 인스턴스 용량이 없음 | 2-1의 용량 예약을 먼저 하고, 예약된 AZ로 apply한다 (2장) |
| `setup_ray.sh`에서 `no response` | 인스턴스가 아직 부팅 중이거나 꺼져 있음 | 몇 분 뒤 다시 실행. 계속되면 EC2 콘솔에서 상태 확인 |
| `setup_ray.sh`에서 `key authentication failed` | head의 SSH 키가 워커에서 받아들여지지 않음 | 워커가 같은 AMI로 떴는지 확인. 다르면 사람에게 알림 |
| 1단계에서 `cannot read s3://...` | 워커가 S3를 못 읽음 | 인스턴스에 IAM instance profile이 붙었는지, 버킷 이름이 맞는지 확인 |
| 1단계에서 `HF_TOKEN not set` | 노드에 토큰이 없음 | `terraform.tfvars`의 `hf_token`이 비어 있지 않은지 확인. 사람에게 알림 |
| 파이프라인 준비가 30분 넘게 진행 없음 | 원격 프로세스 오류 | `results/latest/logs/<구성>/remote/`의 로그 끝부분을 확인해 사람에게 보고. 같은 단계를 다시 실행 |
| 리포트에 `WARNING: this result is not valid` | 한쪽 측정에서 일부 요청이 완료되지 않음 | 해당 측정 단계(3 또는 4)와 5단계를 다시 실행 |
| 1단계를 다시 실행했음 | 새 결과 폴더가 생김 | 정상. 이후 단계는 새 폴더에 기록되므로 2단계부터 다시 진행 |
