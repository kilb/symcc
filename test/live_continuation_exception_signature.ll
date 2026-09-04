; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.json --entry bad_vararg_summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --expect-rejected "unwinding invoke requires bounded internal targets"

declare i32 @__gxx_personality_v0(...)
declare void @__symcc_continuation_throw_if(i1, ...)

define i64 @bad_vararg_summary(i1 %throws)
    personality ptr @__gxx_personality_v0 {
entry:
  invoke void (i1, ...) @__symcc_continuation_throw_if(
      i1 %throws, i64 42)
      to label %normal unwind label %cleanup

normal:
  ret i64 7

cleanup:
  %landing = landingpad { ptr, i32 } cleanup
  resume { ptr, i32 } %landing
}
