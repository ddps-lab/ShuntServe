# 평가용 클러스터 (Terraform)

처리량 향상율 평가에 쓰는 AWS 클러스터를 만든다. 평가 전에 미리 실행해 둔다.
저장소 루트의 `IaC/`와는 별개의 구성이며 상태 파일도 따로 관리된다.

## 만들어지는 자원

| 자원 | 내용 |
|---|---|
| VPC | `192.168.0.0/16`, public subnet 1개 (모든 노드가 같은 가용 영역) |
| Security Group | SSH(22)는 외부 허용, 클러스터 내부 통신은 전부 허용 |
| IAM Instance Profile | `AmazonS3ReadOnlyAccess` (모델 가중치 읽기) |
| Head 노드 | `m5.xlarge` 1대 (옵티마이저와 GlobalServer 실행) |
| Worker 노드 | g6.12xlarge×2 (L4 8장), g5.12xlarge×2 (A10G 8장), g6e.xlarge×4 (L40S 4장) |

모든 인스턴스는 on-demand다. Worker 구성은 `variables.tf`의 `instance_type_count` 기본값이며,
[`../config.json`](../config.json)의 `nodes`와 같아야 한다.

## 사전 준비

- Terraform 1.5 이상, AWS CLI 프로필
- 대상 리전의 EC2 Key Pair
- ShuntServe가 `/home/ubuntu/ShuntServe`에 설치된 AMI (루트 [README.md](../../README.md) 참고).
  head에서 worker로 비밀번호 없이 SSH 접속이 되도록 키가 들어 있어야 한다.
- G 계열 on-demand vCPU 한도 208 이상 (48×4 + 4×4)
- 모델 가중치가 올라가 있는 S3 버킷 (`TensorStore/upload_model.sh`)

## 사용법

```bash
cd Certification/IaC
cp terraform.tfvars.sample terraform.tfvars   # 값 채우기 (gitignore 대상)

./reserve_capacity.sh         # g6.12xlarge×2, g5.12xlarge×2의 용량을 한 AZ에 통째로 예약 (아래 "용량 예약" 참고)
# terraform.tfvars의 availability_zone을 예약된 AZ로 바꾼다

terraform init
terraform plan
terraform apply
./reserve_capacity.sh --cancel   # 워커가 모두 뜬 뒤 예약 취소 (인스턴스는 계속 돈다)

python3 fill_config.py        # worker private IP를 ../config.json에 기록
terraform output head_instance_public_ip
```

### 용량 예약 (`reserve_capacity.sh`)

GPU 용량이 빠듯하면 `terraform apply`가 일부 인스턴스만 띄운 채 나머지를 기다리게 되고, 그동안 뜬 인스턴스 요금이 나간다.
이 스크립트는 AZ마다 g6.12xlarge×2와 g5.12xlarge×2를 On-Demand Capacity Reservation으로 예약해 보고, 하나라도 실패하면 그 AZ에서 잡은 예약을 바로 취소한 뒤 다음 AZ로 넘어간다.
한 AZ에서 4대가 모두 잡히면 그 예약은 유지한 채, 같은 AZ에서 g6e.xlarge×4가 잡힐 때까지 10초 간격으로 다시 시도한다(`FILL_INTERVAL`로 간격 변경). 이때부터 g6.12xlarge와 g5.12xlarge 예약 요금이 나간다.

| 명령 | 내용 |
|---|---|
| `./reserve_capacity.sh` | us-west-2a, b, c를 60초 간격으로 돌며 성공할 때까지 시도 (`INTERVAL=120`으로 간격 변경) |
| `./reserve_capacity.sh us-west-2b us-west-2c` | 지정한 AZ만, 그 순서로 시도 |
| `./reserve_capacity.sh --once` | 한 바퀴만 시도 |
| `./reserve_capacity.sh --dry-run` | 권한과 파라미터만 확인. 용량은 확인하지 않고 아무것도 예약하지 않음 |
| `./reserve_capacity.sh --list` / `--cancel` | 이 스크립트가 만든 예약을 보여 주기 / 모두 취소 |
| `./reserve_capacity.sh --test-notify` | 성공 알림을 한 번 띄워 보기 |

- 성공하거나 용량 부족이 아닌 오류로 멈추면 데스크톱 알림과 소리로 알려 준다(macOS, Linux는 `notify-send`). `NOTIFY=0`이면 끈다. Mac이 잠자기에 들어가면 스크립트도 멈추므로 `caffeinate -i ./reserve_capacity.sh`로 실행한다.
- 이미 이 스크립트의 활성 예약이 있으면 시작하지 않는다. `--list`로 확인하고 `--cancel`로 정리한 뒤 다시 실행한다.

- 실패한 시도는 무료다. 예약은 잡힌 순간부터 취소할 때까지 온디맨드 요금이 나가며, 그 안에서 인스턴스가 돌면 이중으로 과금되지 않는다.
- terraform 워커는 같은 종류, 같은 AZ의 예약에 자동으로 들어간다. head는 예약하지 않는다.
- `terraform destroy`는 예약을 지우지 않는다. 워커가 뜬 뒤 `--cancel`로 취소하고, 정리할 때 `--list`로 남은 예약이 없는지 확인한다.
- 대수는 `../config.json`의 `nodes`에서, AWS 프로필과 리전은 `terraform.tfvars`에서 읽는다.

이후 head 노드에 접속해 사전 준비 스크립트를 실행하고 점검한다.

```bash
ssh ubuntu@<head public ip>
cd ~/ShuntServe/Certification
./prepare/setup_ray.sh    # Ray head 시작, 모든 worker 연결
./prepare/warmup.sh       # 워밍업
./1_check_environment.sh          # 점검
```

`fill_config.py`는 로컬의 `config.json`을 고친다. head 노드에는 [`../README.md`](../README.md)의 사전 준비 1단계(rsync)로 `Certification/` 폴더째 보낸다.

평가가 끝나면 자원을 삭제한다.

```bash
terraform destroy
```

## 변수 (`terraform.tfvars`)

| 변수 | 설명 |
|---|---|
| `prefix` | 자원 이름 앞에 붙는 접두어 |
| `awscli_profile` | AWS CLI 프로필 이름 |
| `ami_id` | ShuntServe가 설치된 AMI |
| `key_name` | EC2 Key Pair 이름 |
| `hf_token` | Llama-3.1-70B-Instruct 접근 권한이 있는 HuggingFace 토큰. 모든 노드의 `HF_TOKEN` 환경 변수로 설정된다 |
| `region`, `availability_zone` | 기본값 `us-west-2`, `us-west-2a`. GPU 인스턴스 용량이 부족하면 가용 영역을 바꾼다 |
| `head_instance_type` | 기본값 `m5.xlarge` |
| `root_volume_size` | 기본값 300 (GB) |

`hf_token`은 인스턴스 user data에 들어가므로, 해당 인스턴스의 user data를 볼 수 있는 계정에서는 값이 보인다.

## 출력

| 출력 | 설명 |
|---|---|
| `head_instance_public_ip`, `head_instance_private_ip` | head 노드 IP |
| `instance_public_ips`, `instance_private_ips` | worker IP (`<인스턴스 종류>-<번호>` 기준) |
| `nodes` | 인스턴스 종류별 worker private IP 목록. `config.json`의 `nodes`와 같은 형태 |
