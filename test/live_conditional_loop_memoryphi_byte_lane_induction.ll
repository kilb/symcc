; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.true.json --entry conditional_loop_true_writer_i8
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.true.json --input-hex 000201 --expect-values 75,75,75,75,75,75,75 --expect-infeasible --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-conditional-loop-memoryphi-byte-lane-induction
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.false.json --entry conditional_loop_false_writer_i8
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.false.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-conditional-loop-memoryphi-byte-lane-induction
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.strided.json --entry conditional_strided_loop_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.strided.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-conditional-loop-memoryphi-byte-lane-induction
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.two-writers.json --entry bad_conditional_loop_two_writers
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.two-writers.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.raw-guard.json --entry bad_conditional_loop_raw_guard
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.raw-guard.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.skip-def.json --entry bad_conditional_loop_skip_definition
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.skip-def.json --expect-rejected "stack load lacks a dominating"
; RUN: %python %S/../benchmark/generate_conditional_loop_memoryphi_byte_lane_fixture.py --object-bytes 64 --stride 8 --writer-bytes 8 --load-bytes 8 --output %t.boundary64.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.boundary64.ll --output %t.boundary64.json --entry generated_conditional_loop_memoryphi_byte_lane
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.boundary64.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-conditional-loop-memoryphi-byte-lane-induction

define i8 @conditional_loop_true_writer_i8(
    i8 %index, i8 %count, i8 %write_limit) {
entry:
  %object = alloca [2 x i8], align 2
  %count64 = zext i8 %count to i64
  %write_limit64 = zext i8 %write_limit to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %decision, label %exit

decision:
  %should_write = icmp ult i64 %iv, %write_limit64
  br i1 %should_write, label %writer, label %skip

writer:
  %write_address = getelementptr inbounds [2 x i8], ptr %object, i64 0, i64 %iv
  store i8 75, ptr %write_address, align 1
  br label %latch

skip:
  br label %latch

latch:
  %next = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [2 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @conditional_loop_false_writer_i8(
    i8 %index, i8 %count, i8 %write_limit) {
entry:
  %object = alloca [4 x i8], align 4
  %count64 = zext i8 %count to i64
  %write_limit64 = zext i8 %write_limit to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %decision, label %exit

decision:
  %skip_write = icmp ult i64 %iv, %write_limit64
  br i1 %skip_write, label %skip, label %writer

writer:
  %write_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 76, ptr %write_address, align 1
  br label %latch

skip:
  br label %latch

latch:
  %next = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i16 @conditional_strided_loop_i16(
    i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %count64 = zext i8 %count to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %decision, label %exit

decision:
  %should_write = icmp ne i64 %iv, 2
  br i1 %should_write, label %writer, label %skip

writer:
  %write_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %iv
  store i16 19789, ptr %write_address, align 1
  br label %latch

skip:
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

define i8 @bad_conditional_loop_two_writers(
    i8 %index, i8 %count, i8 %write_limit) {
entry:
  %object = alloca [4 x i8], align 4
  %count64 = zext i8 %count to i64
  %write_limit64 = zext i8 %write_limit to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %decision, label %exit

decision:
  %choose = icmp ult i64 %iv, %write_limit64
  br i1 %choose, label %writer_a, label %writer_b

writer_a:
  %address_a = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 1, ptr %address_a, align 1
  br label %latch

writer_b:
  %address_b = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 2, ptr %address_b, align 1
  br label %latch

latch:
  %next = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @bad_conditional_loop_raw_guard(
    i8 %index, i8 %count, i1 %should_write) {
entry:
  %object = alloca [4 x i8], align 4
  %count64 = zext i8 %count to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %decision, label %exit

decision:
  br i1 %should_write, label %writer, label %skip

writer:
  %write_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 3, ptr %write_address, align 1
  br label %latch

skip:
  br label %latch

latch:
  %next = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @bad_conditional_loop_skip_definition(
    i8 %index, i8 %count, i8 %write_limit) {
entry:
  %object = alloca [4 x i8], align 4
  %count64 = zext i8 %count to i64
  %write_limit64 = zext i8 %write_limit to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next, %latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %decision, label %exit

decision:
  %should_write = icmp ult i64 %iv, %write_limit64
  br i1 %should_write, label %writer, label %skip

writer:
  %write_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 4, ptr %write_address, align 1
  br label %latch

skip:
  store i8 0, ptr %object, align 1
  br label %latch

latch:
  %next = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}
