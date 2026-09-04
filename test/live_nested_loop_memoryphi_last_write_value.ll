; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.ordered.json --entry nested_last_write_value_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.ordered.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.ordered.json --input-hex 000103 --expect-values 4660,4660,4660,4660,4660,4660 --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.ordered.json --input-hex 010103 --expect-values 0,0,0,13330,13330,13330 --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.singleton.json --entry nested_singleton_value_i8
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.singleton.json --input-hex 000101 --expect-values 255,255,255,255,255,255 --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.dynamic.json --entry nested_dynamic_value_fallback_i8
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.dynamic.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --reject-capability bounded-nested-loop-memoryphi-last-write-value-summary
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.nonbyte.json --entry nested_non_byte_constant_fallback_i1
; RUN: %python -c "import json; data=json.load(open(r'%t.nonbyte.json')); assert data['schema'] == 'symcc-llvm-continuation-lowering-v1'; assert data['status'] == 'rejected'; assert any('lacks a dominating full-width initializing store' in item for item in data['diagnostics'])"
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_value_summary_fixture.py --object-bytes 64 --inner-step 8 --writer-widths 1,2,4,8 --writer-values 17,4660,305419896,72623859790382856 --load-bytes 8 --output %t.boundary64.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.boundary64.ll --output %t.boundary64.json --entry generated_nested_loop_memoryphi_summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.boundary64.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary

define i16 @nested_last_write_value_i16(
    i8 %index, i8 %outer_count, i8 %inner_count) {
entry:
  %object = alloca [8 x i8], align 8
  %outer_count64 = zext i8 %outer_count to i64
  %inner_count64 = zext i8 %inner_count to i64
  br label %outer_header

outer_header:
  %outer_iv = phi i64 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i64 %outer_iv, %outer_count64
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i64 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i64 %inner_iv, %inner_count64
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %write_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %inner_iv
  store i8 -86, ptr %write_address, align 1
  store i16 4660, ptr %write_address, align 1
  %inner_next = add nuw i64 %inner_iv, 2
  br label %inner_header

outer_latch:
  %outer_next = add nuw i64 %outer_iv, 1
  br label %outer_header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %index64
  %value = load i16, ptr %read_address, align 1
  ret i16 %value
}

define i8 @nested_singleton_value_i8(
    i8 %index, i8 %outer_count, i8 %inner_count) {
entry:
  %object = alloca [4 x i8], align 4
  %outer_count64 = zext i8 %outer_count to i64
  %inner_count64 = zext i8 %inner_count to i64
  br label %outer_header

outer_header:
  %outer_iv = phi i64 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i64 %outer_iv, %outer_count64
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i64 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i64 %inner_iv, %inner_count64
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %write_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %inner_iv
  store i8 -1, ptr %write_address, align 1
  %inner_next = add nuw i64 %inner_iv, 1
  br label %inner_header

outer_latch:
  %outer_next = add nuw i64 %outer_iv, 1
  br label %outer_header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @nested_dynamic_value_fallback_i8(
    i8 %index, i8 %outer_count, i8 %inner_count) {
entry:
  %object = alloca [4 x i8], align 4
  %outer_count64 = zext i8 %outer_count to i64
  %inner_count64 = zext i8 %inner_count to i64
  br label %outer_header

outer_header:
  %outer_iv = phi i64 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i64 %outer_iv, %outer_count64
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i64 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i64 %inner_iv, %inner_count64
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %write_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %inner_iv
  %dynamic_value = trunc i64 %inner_iv to i8
  store i8 %dynamic_value, ptr %write_address, align 1
  %inner_next = add nuw i64 %inner_iv, 1
  br label %inner_header

outer_latch:
  %outer_next = add nuw i64 %outer_iv, 1
  br label %outer_header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i1 @nested_non_byte_constant_fallback_i1(
    i8 %index, i8 %outer_count, i8 %inner_count) {
entry:
  %object = alloca [4 x i8], align 4
  %outer_count64 = zext i8 %outer_count to i64
  %inner_count64 = zext i8 %inner_count to i64
  br label %outer_header

outer_header:
  %outer_iv = phi i64 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i64 %outer_iv, %outer_count64
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i64 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i64 %inner_iv, %inner_count64
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %write_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %inner_iv
  store i1 true, ptr %write_address, align 1
  %inner_next = add nuw i64 %inner_iv, 1
  br label %inner_header

outer_latch:
  %outer_next = add nuw i64 %outer_iv, 1
  br label %outer_header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i1, ptr %read_address, align 1
  ret i1 %value
}
