; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %s --output %t.json --entry nested_decision_dag_value_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --validate-only --expect-stack --expect-alias --expect-nested-loop-memoryphi-decision-dag-value-summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 0001033412 --expect-values 13994,13994,13994,13994,13994,13994 --expect-stack --expect-alias --expect-nested-loop-memoryphi-decision-dag-value-summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 0803033412 --expect-values 0,0,0,0,9898,9898 --expect-stack --expect-alias --expect-nested-loop-memoryphi-decision-dag-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_decision_dag_fixture.py --endianness big --no-shared-leaf --inner-predicate ne --root-predicate uge --output %t.big.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.big.ll --output %t.big.json --entry generated_nested_loop_memoryphi_decision_dag
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.big.json --validate-only --expect-stack --expect-alias --expect-nested-loop-memoryphi-decision-dag-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_decision_dag_fixture.py --mixed-piecewise-writer --output %t.mixed.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.mixed.ll --output %t.mixed.json --entry generated_nested_loop_memoryphi_decision_dag
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.mixed.json --validate-only --expect-stack --expect-alias --expect-nested-loop-memoryphi-decision-dag-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_decision_dag_fixture.py --shared-subtree --output %t.subtree.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.subtree.ll --output %t.subtree.json --entry generated_nested_loop_memoryphi_decision_dag
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.subtree.json --validate-only --expect-stack --expect-alias --expect-nested-loop-memoryphi-decision-dag-value-summary

define i16 @nested_decision_dag_value_i16(
    i4 %index, i2 %outer_count, i2 %inner_count, i16 %payload) {
entry:
  %object = alloca [12 x i8], align 4
  %outer_bound = zext i2 %outer_count to i16
  %inner_bound = zext i2 %inner_count to i16
  br label %outer_header

outer_header:
  %outer_iv = phi i16 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i16 %outer_iv, %outer_bound
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i16 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i16 %inner_iv, %inner_bound
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %row = mul nuw i16 %outer_iv, 4
  %flat = add nuw i16 %row, %inner_iv
  %write_address = getelementptr inbounds [12 x i8], ptr %object, i16 0, i16 %flat
  %a_input = mul i16 %payload, 3
  %a_outer = mul i16 %outer_iv, 5
  %a_inner = mul i16 %inner_iv, 7
  %a_partial = add i16 %a_input, %a_outer
  %a_sum = add i16 %a_partial, %a_inner
  %value_a = add i16 %a_sum, 17
  %b_input = mul i16 %payload, 2
  %b_outer = mul i16 %outer_iv, 11
  %b_inner = mul i16 %inner_iv, 13
  %b_partial = add i16 %b_input, %b_outer
  %b_sum = add i16 %b_partial, %b_inner
  %value_b = add i16 %b_sum, 513
  %inner_guard = icmp ult i16 %inner_iv, 2
  %inner_selected = select i1 %inner_guard, i16 %value_a, i16 %value_b
  %outer_guard = icmp ule i16 %outer_iv, 1
  %stored = select i1 %outer_guard, i16 %inner_selected, i16 %value_b
  store i16 %stored, ptr %write_address, align 1
  store i8 -86, ptr %write_address, align 1
  %inner_next = add nuw i16 %inner_iv, 2
  br label %inner_header

outer_latch:
  %outer_next = add nuw i16 %outer_iv, 1
  br label %outer_header

exit:
  %index16 = zext i4 %index to i16
  %read_address = getelementptr inbounds [12 x i8], ptr %object, i16 0, i16 %index16
  %value = load i16, ptr %read_address, align 1
  ret i16 %value
}
