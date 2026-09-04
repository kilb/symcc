; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.loop.json --entry loop_memoryphi_byte_lane_induction
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.loop.json --input-hex 0204 --expect-values 0,0,16705,16705,16705,16705,16705 --expect-infeasible --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.partial.json --entry bad_loop_memoryphi_partial
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.partial.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.step.json --entry bad_loop_memoryphi_nonunit_step
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.step.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.bound.json --entry bad_loop_memoryphi_narrow_bound
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.bound.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.second-writer.json --entry bad_loop_memoryphi_second_writer
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.second-writer.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.extra-phi.json --entry bad_loop_memoryphi_extra_phi
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.extra-phi.json --expect-rejected "stack load lacks a dominating"
; RUN: %python %S/../benchmark/generate_loop_memoryphi_byte_lane_fixture.py --object-bytes 64 --load-bytes 8 --output %t.boundary64.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.boundary64.ll --output %t.boundary64.json --entry generated_loop_memoryphi_byte_lane
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.boundary64.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction

define i16 @loop_memoryphi_byte_lane_induction(i8 %index, i8 %count) {
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
  store i8 65, ptr %write_address, align 1
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

define i8 @bad_loop_memoryphi_partial(i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %count64 = zext i8 %count to i64
  br label %header

header:
  %iv = phi i64 [ 1, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %body, label %exit

body:
  %write_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %iv
  store i8 66, ptr %write_address, align 1
  br label %latch

latch:
  %next = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @bad_loop_memoryphi_nonunit_step(i8 %index, i8 %count) {
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
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @bad_loop_memoryphi_narrow_bound(i8 %index, i2 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %count64 = zext i2 %count to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %body, label %exit

body:
  %write_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %iv
  store i8 68, ptr %write_address, align 1
  br label %latch

latch:
  %next = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @bad_loop_memoryphi_second_writer(i8 %index, i8 %count) {
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
  store i8 70, ptr %write_address, align 1
  br label %latch

latch:
  %next = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @bad_loop_memoryphi_extra_phi(i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %count64 = zext i8 %count to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %shadow = phi i8 [ 7, %entry ], [ %shadow_next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %body, label %exit

body:
  %write_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %iv
  store i8 71, ptr %write_address, align 1
  br label %latch

latch:
  %next = add nuw i64 %iv, 1
  %shadow_next = add i8 %shadow, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  %result = add i8 %value, %shadow
  ret i8 %result
}
