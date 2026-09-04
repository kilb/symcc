; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.integer.json --entry ssa_copy_integer
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.integer.json --input-hex 05 --expect-values 1,2 --expect-ssa-copy-intrinsic
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.pointer.json --entry ssa_copy_pointer
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.pointer.json --input-hex 00 --expect-values 1,2 --expect-ssa-copy-intrinsic --expect-pointer-union
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.function.json --entry ssa_copy_function
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.function.json --input-hex 0005 --expect-values 6,7 --expect-ssa-copy-intrinsic --expect-indirect-call
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.poison.json --entry freeze_transitive_ssa_copy
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.poison.json --expect-values 1,2 --expect-deferred-poison-freeze --expect-transitive-deferred-poison --expect-ssa-copy-intrinsic

@copy_left = global i8 65, align 1
@copy_right = global i8 66, align 1

declare i8 @llvm.ssa.copy.i8(i8 returned)
declare ptr @llvm.ssa.copy.p0(ptr returned)

define i8 @increment(i8 %value) {
entry:
  %next = add i8 %value, 1
  ret i8 %next
}

define i8 @increment_two(i8 %value) {
entry:
  %next = add i8 %value, 2
  ret i8 %next
}

define i8 @ssa_copy_integer(i8 %value) {
entry:
  %copy = call i8 @llvm.ssa.copy.i8(i8 %value)
  %matched = icmp eq i8 %copy, 5
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @ssa_copy_pointer(i1 %choose) {
entry:
  %selected = select i1 %choose, ptr @copy_left, ptr @copy_right
  %copy = call ptr @llvm.ssa.copy.p0(ptr %selected)
  %value = load i8, ptr %copy, align 1
  %matched = icmp eq i8 %value, 65
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}

define i8 @ssa_copy_function(i1 %choose, i8 %value) {
entry:
  %selected = select i1 %choose, ptr @increment, ptr @increment_two
  %copy = call ptr @llvm.ssa.copy.p0(ptr %selected)
  %result = call i8 %copy(i8 %value)
  ret i8 %result
}

define i8 @freeze_transitive_ssa_copy() {
entry:
  %overflow = add nsw i8 127, 1
  %copy = call i8 @llvm.ssa.copy.i8(i8 %overflow)
  %stable = freeze i8 %copy
  %matched = icmp eq i8 %stable, 42
  br i1 %matched, label %yes, label %no

yes:
  ret i8 1

no:
  ret i8 2
}
