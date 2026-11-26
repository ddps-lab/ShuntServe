# records

head 노드에서 가져온 실행 결과를 보관하는 폴더다. 평가 절차에는 포함되지 않는다.

head 노드에서는 실행 결과가 평가 때와 같은 경로(`Certification/results/<실행 시각>/`)에 기록되고,
가져올 때 이 폴더(`records/<실행 시각>/`)에 넣는다. 폴더 구조는 head의 결과 폴더와 같다.

```bash
# 내 컴퓨터에서: head의 가장 최근 실행을 가져오기 (이름을 지정하면 그 실행을 가져온다)
./records/fetch.sh <head 주소> [실행 이름]

# 저장된 결과를 5_report.sh와 같은 형식으로 출력 (아무것도 쓰지 않는다)
./show_result.sh                # records/의 가장 최근 실행
./show_result.sh <실행 이름>
```

각 실행 폴더에는 그 실행을 시작할 때의 `config.json` 사본이 들어 있어서, 현재 설정이 바뀌어도 당시 조건으로 출력된다.
