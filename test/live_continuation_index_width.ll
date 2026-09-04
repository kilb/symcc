; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.json --entry bad_index_width
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --expect-rejected "pointer/index width mismatch"

target datalayout = "e-p:64:64:64:32"

@bytes = constant [4 x i8] c"ABCD", align 4

define i8 @bad_index_width(i64 %index) {
entry:
  %address = getelementptr [4 x i8], ptr @bytes, i64 0, i64 %index
  %value = load i8, ptr %address, align 1
  ret i8 %value
}
