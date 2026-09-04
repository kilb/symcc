; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_decision_dag_fixture.py --output %t.valid.ll
; RUN: sed 's/%inner_guard = icmp ult i16/%inner_guard = icmp slt i16/' %t.valid.ll > %t.signed.ll
; RUN: not env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.signed.ll --output %t.signed.json --entry generated_nested_loop_memoryphi_decision_dag
; RUN: %python -c "import json; data=json.load(open(r'%t.signed.json')); assert data['status'] == 'rejected'; assert any('lacks a dominating full-width initializing store' in item for item in data['diagnostics'])"
; RUN: sed 's/%inner_guard = icmp ult i16 %inner_iv, 2/%inner_guard = icmp ult i16 %%payload, 2/' %t.valid.ll > %t.input.ll
; RUN: not env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.input.ll --output %t.input.json --entry generated_nested_loop_memoryphi_decision_dag
; RUN: %python -c "import json; data=json.load(open(r'%t.input.json')); assert data['status'] == 'rejected'; assert any('lacks a dominating full-width initializing store' in item for item in data['diagnostics'])"

; This file exercises producer fail-closed paths using generated modules.
