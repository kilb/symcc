; RUN: not env SYMCC_UCSAN_ENTRY=callee %symcc -O0 %s -o %t 2>&1 | FileCheck %s

declare i32 @target(i32)

define i32 @callee(i32 %value) {
entry:
  %result = musttail call i32 @target(i32 %value)
  ret i32 %result
}

; CHECK: SymCC UCSan: musttail is unsupported in scoped function: callee
; CHECK: fatal error: error in backend: unsupported UCSan musttail exit
