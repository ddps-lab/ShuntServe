# 평가 데이터셋

같은 trace에서 구간 길이만 다르게 잘라 낸 두 파일을 둔다. 평가에는 [`../config.json`](../config.json)의 `workload.dataset`이 가리키는 파일 하나를 쓰며, 현재는 **3분 구간**이다.

| 파일 | 구간 | 요청 수 | 용도 |
|---|---|---|---|
| `azure_conv_2023_input2048_first180s.csv` | 처음 180초 (3분) | 727 | **현재 평가에 사용** |
| `azure_conv_2023_input2048_first300s.csv` | 처음 300초 (5분) | 1,309 | 예비. 논문 실험과 같은 구간 |

3분 파일은 5분 파일의 앞부분 727행과 똑같다. 두 구성(기준 구성, 이기종 GPU 구성) 모두 선택한 파일의 요청을 그대로 사용한다.

5분 구간으로 바꾸려면 `config.json`에서 `workload.dataset`을 5분 파일로, `workload.window_s`를 300으로 바꾼다.

## 출처와 정제 과정

원본은 Azure LLM Inference Trace 2023의 대화(Conversation) trace이며, 저장소의
`ArtifactEvaluation/Datasets/AzureLLMInferenceTrace_conv.csv`에 있다.

| 단계 | 내용 | 요청 수 |
|---|---|---|
| 원본 | Azure LLM Inference Conversation Trace 2023 | 19,366 |
| 1 | 입력이 2048 토큰을 넘는 요청 제외 (`ContextTokens > 2048`) | 16,663 |
| 2a | trace 시작부터 180초 안에 도착한 요청만 사용 | 727 |
| 2b | trace 시작부터 300초 안에 도착한 요청만 사용 | 1,309 |

각 행은 원본에서 그대로 복사했고 순서도 같다. 값을 바꾸거나 새로 만든 행은 없다.

두 파일은 `build_dataset.py`로 다시 만들 수 있다.

```bash
python3 build_dataset.py
```

## 형식

| 열 | 의미 |
|---|---|
| `TIMESTAMP` | 요청 도착 시각 |
| `ContextTokens` | 입력 토큰 수 |
| `GeneratedTokens` | 출력 토큰 수 |

## 요약

| 항목 | 3분 (180초) | 5분 (300초) |
|---|---|---|
| 요청 수 | 727 | 1,309 |
| 입력 토큰 (평균 / 최대) | 739.4 / 2,042 | 775.8 / 2,042 |
| 출력 토큰 (평균 / 최대) | 274.9 / 1,000 | 273.4 / 1,000 |
| 전체 토큰 (입력 / 출력) | 537,553 / 199,834 | 1,015,532 / 357,872 |

평가에서는 도착 시각 간격을 쓰지 않고 모든 요청을 한 번에 제출한다. 요청 본문은 토큰 수에 맞춰 임의 토큰으로 생성하며(시드 고정),
출력은 `GeneratedTokens` 개수만큼 생성하도록 한다.
