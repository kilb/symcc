; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.step.json --entry strided_step_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.step.json --input-hex 0204 --expect-values 0,16961,16961,16961 --expect-infeasible --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.scale.json --entry strided_scale_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.scale.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.residue.json --entry strided_partial_residue_i8
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.residue.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.gap.json --entry bad_strided_residue_gap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.gap.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.overlap.json --entry bad_strided_overlap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.overlap.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.wrap.json --entry bad_strided_wrap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.wrap.json --expect-rejected "stack load lacks a dominating"
; RUN: %python %S/../benchmark/generate_strided_loop_memoryphi_byte_lane_fixture.py --object-bytes 64 --stride 8 --writer-bytes 8 --load-bytes 8 --output %t.boundary64.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.boundary64.ll --output %t.boundary64.json --entry generated_strided_loop_memoryphi_byte_lane
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.boundary64.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction

define i16 @strided_step_i16(i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %count64 = zext i8 %count to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %body, label %exit

body:
  %write_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %iv
  store i16 16961, ptr %write_address, align 1
  br label %latch

latch:
  %next = add nuw i64 %iv, 2
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %index64
  %value = load i16, ptr %read_address, align 1
  ret i16 %value
}

define i16 @strided_scale_i16(i8 %index, i8 %count) {
entry:
  %object = alloca [4 x i16], align 8
  %count64 = zext i8 %count to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %body, label %exit

body:
  %write_address = getelementptr inbounds [4 x i16], ptr %object, i64 0, i64 %iv
  store i16 17218, ptr %write_address, align 2
  br label %latch

latch:
  %next = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds i8, ptr %object, i64 %index64
  %value = load i16, ptr %read_address, align 1
  ret i16 %value
}

define i8 @strided_partial_residue_i8(i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %count64 = zext i8 %count to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %body, label %exit

body:
  %write_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %iv
  store i8 69, ptr %write_address, align 1
  br label %latch

latch:
  %next = add nuw i64 %iv, 2
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i16], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i16 @bad_strided_residue_gap(i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %count64 = zext i8 %count to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %body, label %exit

body:
  %write_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %iv
  store i8 67, ptr %write_address, align 1
  br label %latch

latch:
  %next = add nuw i64 %iv, 2
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %index64
  %value = load i16, ptr %read_address, align 1
  ret i16 %value
}

define i16 @bad_strided_overlap(i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %count64 = zext i8 %count to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %body, label %exit

body:
  %write_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %iv
  store i16 17475, ptr %write_address, align 1
  br label %latch

latch:
  %next = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %index64
  %value = load i16, ptr %read_address, align 1
  ret i16 %value
}

define i16 @bad_strided_wrap(i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  br label %header

header:
  %iv = phi i8 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i8 %iv, %count
  br i1 %continue, label %body, label %exit

body:
  %write_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i8 %iv
  store i16 17732, ptr %write_address, align 1
  br label %latch

latch:
  %next = add nuw i8 %iv, 2
  br label %header

exit:
  %read_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i8 %index
  %value = load i16, ptr %read_address, align 1
  ret i16 %value
}
