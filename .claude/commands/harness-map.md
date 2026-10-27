하네스 구조를 HTML로 출력하고 Health 결과를 요약하라.

1. `python3 scripts/harness_map.py $ARGUMENTS`를 실행한다. (글로벌 레이어까지 보려면 `--with-global`)
2. 생성된 `harness_map.html` 경로와 Summary 수치(완료 step, 요구사항 커버리지, 제안 대기/승인)를 보고한다.
3. Health의 error와 warn을 표로 정리하고, 각 항목의 수정 방안을 제시한다.
   - 수정 방안은 하네스 설정·문서(step 파일, PRD, hooks) 범위로 한정한다. 제품 기능 추가는 제안하지 않는다.
   - 직접 수정하지 말고 사용자 승인을 받은 뒤 수정한다.
